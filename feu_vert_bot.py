"""
FEU VERT BOT v4 — même logique que l'indicateur TradingView « Feu Vert Scalping » v4.
Bougies Kraken (5 min, 15 min, H1, H4) + profil de volume, alerte Telegram dès qu'un
voyant passe au vert. Commande /etat : état complet au passage suivant.
Longs uniquement, scalping 1 à 3 %. Aide à la décision, pas une garantie.
"""
import json
import os
import sys
import time
import urllib.parse
import urllib.request

COINS = {"BTC": "XBTUSD", "TAO": "TAOUSD", "ZEC": "ZECUSD", "SOL": "SOLUSD"}
TIMEFRAMES = {"5 min": 5, "15 min": 15, "H1": 60, "H4": 240}
GO_SCORE = 70
MIN_PROB = 65.0
HORIZON = 24
MIN_STOP = 1.5
MAX_STOP = 4.0
TARGETS = [3.0, 2.5, 2.0, 1.5, 1.0]
VP_LEN = 100
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state.json")

TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
FORCE_SUMMARY = os.environ.get("FORCE_SUMMARY", "") == "1"


def get_json(url, tries=3):
    for k in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "feu-vert-bot/1.0"})
            with urllib.request.urlopen(req, timeout=20) as r:
                return json.loads(r.read().decode())
        except Exception:
            if k == tries - 1:
                raise
            time.sleep(2 + 2 * k)


def candles(pair, interval):
    data = get_json(f"https://api.kraken.com/0/public/OHLC?pair={pair}&interval={interval}")
    if data.get("error"):
        raise RuntimeError(", ".join(data["error"]))
    key = next(k for k in data["result"] if k != "last")
    rows = data["result"][key][:-1]
    return [(int(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[6])) for r in rows]


def ema(a, n):
    k = 2 / (n + 1)
    out = [a[0]]
    for v in a[1:]:
        out.append(v * k + out[-1] * (1 - k))
    return out


def rma(a, n):
    out = [None] * len(a)
    if len(a) < n:
        return out
    s = sum(a[:n]) / n
    out[n - 1] = s
    for i in range(n, len(a)):
        s = (s * (n - 1) + a[i]) / n
        out[i] = s
    return out


def rsi(c, n=14):
    up = [0.0] + [max(c[i] - c[i - 1], 0) for i in range(1, len(c))]
    dn = [0.0] + [max(c[i - 1] - c[i], 0) for i in range(1, len(c))]
    au, ad = rma(up, n), rma(dn, n)
    return [None if au[i] is None else (100.0 if ad[i] == 0 else 100 - 100 / (1 + au[i] / ad[i])) for i in range(len(c))]


def sma_opt(a, n):
    out = [None] * len(a)
    for i in range(len(a)):
        w = a[max(0, i - n + 1): i + 1]
        if len(w) == n and all(v is not None for v in w):
            out[i] = sum(w) / n
    return out


def stoch_of(a, n=14):
    out = [None] * len(a)
    for i in range(len(a)):
        w = a[max(0, i - n + 1): i + 1]
        if len(w) == n and all(v is not None for v in w):
            lo, hi = min(w), max(w)
            out[i] = 50.0 if hi == lo else 100 * (a[i] - lo) / (hi - lo)
    return out


def vwma(c, v, n=50):
    out = [None] * len(c)
    for i in range(n - 1, len(c)):
        sv = sum(v[i - n + 1: i + 1])
        out[i] = sum(c[j] * v[j] for j in range(i - n + 1, i + 1)) / sv if sv > 0 else c[i]
    return out


def atr(h, l, c, n=14):
    tr = [h[0] - l[0]] + [max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1])) for i in range(1, len(c))]
    return rma(tr, n)


def volume_profile(rows, bins=24):
    """POC (prix le plus échangé) + zone de valeur 70 % (VAL = bas, VAH = haut)."""
    hi = max(r[2] for r in rows); lo = min(r[3] for r in rows)
    if hi <= lo:
        return None
    step = (hi - lo) / bins
    vols = [0.0] * bins
    for r in rows:
        tp = (r[2] + r[3] + r[4]) / 3
        vols[min(bins - 1, max(0, int((tp - lo) / step)))] += r[5]
    p = vols.index(max(vols)); a = b = p; acc = vols[p]; total = sum(vols)
    while acc < total * 0.7 and (a > 0 or b < bins - 1):
        dn = vols[a - 1] if a > 0 else -1
        upv = vols[b + 1] if b < bins - 1 else -1
        if upv >= dn:
            b += 1; acc += vols[b]
        else:
            a -= 1; acc += vols[a]
    return lo + step * (p + 0.5), lo + step * a, lo + step * (b + 1)


def vp_bonus(price, poc, val, vah):
    """Le profil de volume fait pencher la balance : bas de zone = +, haut de zone = −."""
    if price < val * 0.995:
        return -10, "sous la zone de valeur (cassure)"
    if price > vah:
        return -15, "au-dessus de la zone de valeur"
    if price >= vah - 0.15 * (vah - val):
        return -10, "collé au haut de zone"
    if price <= val + 0.25 * (poc - val):
        return 15, "bas de zone de valeur"
    if price <= poc:
        return 8, "sous le POC"
    return 0, "entre POC et haut de zone"


def analyse(rows):
    h = [r[2] for r in rows]; l = [r[3] for r in rows]
    c = [r[4] for r in rows]; v = [r[5] for r in rows]
    n = len(c)
    e20, e50 = ema(c, 20), ema(c, 50)
    r = rsi(c)
    k = sma_opt(stoch_of(r), 3)
    d = sma_opt(k, 3)
    vw = vwma(c, v)
    a = atr(h, l, c)

    score, up, sp = [None] * n, [False] * n, [None] * n
    for i in range(1, n):
        if None in (r[i], r[i - 1], k[i], d[i], vw[i], a[i]):
            continue
        s = 0
        s += 15 if c[i] > e50[i] else 0
        s += 15 if e20[i] > e50[i] else 0
        s += 20 if r[i] <= 50 else 10 if r[i] <= 60 else 0
        s += 15 if k[i] < 25 else 10 if (k[i] > d[i] and k[i] < 50) else 0
        s += 15 if c[i] <= vw[i] else 0
        s += 10 if (r[i] > r[i - 1] and r[i] < 60) else 0
        score[i] = round(s * 100 / 90)
        up[i] = c[i] > e50[i] and e20[i] > e50[i]
        sp[i] = max(MIN_STOP / 100, min(MAX_STOP / 100, 1.5 * a[i] / c[i]))

    wins = [0] * len(TARGETS); nset = 0
    for i in range(60, n - HORIZON):
        if score[i] is None or not (score[i] >= 60 and up[i]):
            continue
        nset += 1
        e, s0 = c[i], sp[i]
        for j, t in enumerate(TARGETS):
            for m in range(i + 1, i + HORIZON + 1):
                if l[m] <= e * (1 - s0):
                    break
                if h[m] >= e * (1 + t / 100):
                    wins[j] += 1
                    break

    last = n - 1
    stop_pct = sp[last]
    pick = None
    for j, t in enumerate(TARGETS):
        p = wins[j] / nset if nset else 0.0
        ev = p * t - (1 - p) * stop_pct * 100
        if p * 100 >= MIN_PROB and ev > 0:
            pick = (t, p, ev)
            break
    entry = c[last]
    poc = val = vah = None
    bonus, vp_txt = 0, "profil indisponible"
    vp = volume_profile(rows[-VP_LEN:])
    if vp:
        poc, val, vah = vp
        bonus, vp_txt = vp_bonus(entry, poc, val, vah)
    final = None if score[last] is None else max(0, min(100, score[last] + bonus))

    target_pct, vp_target = (pick[0] if pick else None), False
    if pick and vp:
        for lvl in (poc, vah):
            pct = (lvl * 0.999 / entry - 1) * 100
            if 1.0 <= pct < target_pct:
                target_pct, vp_target = pct, True
                break
    green = final is not None and final >= GO_SCORE and up[last] and pick is not None
    return {
        "score": final, "up": up[last], "green": green,
        "entry": entry, "stop": entry * (1 - stop_pct), "stop_pct": stop_pct * 100,
        "target": entry * (1 + target_pct / 100) if pick else None,
        "target_pct": target_pct, "prob": pick[1] * 100 if pick else None,
        "vp_target": vp_target, "cases": nset,
        "poc": poc, "val": val, "vah": vah, "vp_txt": vp_txt, "vp_bonus": bonus,
    }


def fmt(x):
    if x is None:
        return "—"
    d = 0 if x >= 1000 else 1 if x >= 100 else 2 if x >= 1 else 4
    return f"{x:,.{d}f}".replace(",", " ").replace(".", ",")


def send(text):
    if not TOKEN or not CHAT_ID:
        print("[telegram non configuré]\n" + text)
        return
    body = urllib.parse.urlencode({"chat_id": CHAT_ID, "text": text, "disable_web_page_preview": "true"}).encode()
    try:
        urllib.request.urlopen(f"https://api.telegram.org/bot{TOKEN}/sendMessage", data=body, timeout=20)
    except Exception as e:
        print(f"[échec Telegram] {e}")


def check_commands(state):
    """Lit les messages reçus depuis le dernier passage : /etat demande l'état complet."""
    if not TOKEN or not CHAT_ID:
        return False
    asked = False
    try:
        if not state.get("_menu"):
            cmds = json.dumps([{"command": "etat", "description": "État des 16 voyants (sous 5 min)"}])
            urllib.request.urlopen(f"https://api.telegram.org/bot{TOKEN}/setMyCommands",
                                   data=urllib.parse.urlencode({"commands": cmds}).encode(), timeout=20)
            state["_menu"] = True
        off = state.get("_offset", 0)
        data = get_json(f"https://api.telegram.org/bot{TOKEN}/getUpdates?offset={off + 1}&timeout=0")
        for u in data.get("result", []):
            state["_offset"] = max(state.get("_offset", 0), u["update_id"])
            m = u.get("message") or {}
            txt = (m.get("text") or "").strip().lower()
            if str(m.get("chat", {}).get("id")) == str(CHAT_ID) and txt.lstrip("/").startswith(("etat", "état")):
                asked = True
    except Exception as e:
        print(f"[commandes Telegram] {e}")
    return asked


def line(tf, res, new):
    pc = lambda x: f"{x:.1f}".replace(".", ",")
    tgt_note = " (sur POC/haut de zone)" if res["vp_target"] else ""
    prob = f"≥ {res['prob']:.0f}" if res["vp_target"] else f"{res['prob']:.0f}"
    return (f"🟢 {tf}{' (nouveau)' if new else ''} : entrée {fmt(res['entry'])} · "
            f"cible {fmt(res['target'])} (+{pc(res['target_pct'])} %){tgt_note} · "
            f"stop {fmt(res['stop'])} (−{pc(res['stop_pct'])} %) · "
            f"proba {prob} % sur {res['cases']} cas\n"
            f"     volume : {res['vp_txt']} · zone {fmt(res['val'])} – {fmt(res['vah'])} · POC {fmt(res['poc'])}")


def main():
    try:
        with open(STATE_FILE) as f:
            state = json.load(f)
    except Exception:
        state = {}

    asked = check_commands(state)
    summary, errors = [], []
    for coin, pair in COINS.items():
        results = {}
        for tf, iv in TIMEFRAMES.items():
            try:
                results[tf] = analyse(candles(pair, iv))
            except Exception as e:
                errors.append(f"{coin} {tf} : {e}")
            time.sleep(1.1)
        if not results:
            continue

        prev = state.get(coin, {})
        new = {tf: r["green"] and not prev.get(tf, False) for tf, r in results.items()}
        greens = [tf for tf, r in results.items() if r["green"]]
        h4_up = results.get("H4", {}).get("up", False)

        if any(new.values()):
            msg = f"FEU VERT {coin} · {len(greens)} unité(s) verte(s)"
            if not h4_up:
                msg += " · attention : tendance H4 pas haussière"
            msg += "\n" + "\n".join(line(tf, results[tf], new[tf]) for tf in greens)
            send(msg)
            print(msg)

        state[coin] = {tf: r["green"] for tf, r in results.items()}
        lights = " ".join(f"{tf}:{'🟢' if r['green'] else '🔴'}{r['score']}" for tf, r in results.items())
        summary.append(f"{coin}  {lights}  · H4 {'↑' if h4_up else '↓'}")

    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=1, sort_keys=True)

    report = "Feu Vert — état actuel\n" + "\n".join(summary)
    if errors:
        report += "\nErreurs : " + " | ".join(errors)
    print(report)
    if FORCE_SUMMARY or asked:
        send(report)


if __name__ == "__main__":
    sys.exit(main())
