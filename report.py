"""Builds the plain-English report, prints it and writes REPORT.md.

python3 report.py
"""
import csv
import datetime
import os
import statistics

import config
import net
import paper

# Skips about the desk's limits or timing, not the coin: the filter check leaves them out.
DESK_LIMITS = ("max_positions", "daily_limit", "out_of_cash", "late", "leader_already_sold")
FOOTER = ("Not real money. Fills include slippage and fees to stay honest, "
          "but real trades would land later than the leader's and could be worse.")


def read_csv(path):
    if not os.path.exists(path):
        return []
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


def num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def parse_time(s):
    try:
        return datetime.datetime.strptime(s, "%Y-%m-%d %H:%M:%S").replace(
            tzinfo=datetime.timezone.utc).timestamp()
    except (TypeError, ValueError):
        return None


def first_word(reason):
    return (reason or "").split(" ")[0].rstrip(":") or "?"


def money(x):
    return ("-$%.2f" % -x) if x < 0 else ("$%.2f" % x)


def signed_money(x):
    return ("+" if x >= 0 else "") + money(x)


def pct(x):
    return "%+.1f%%" % x


def episodes(trades):
    """Group trades into per-coin holdings: a BUY and all the sells after it."""
    open_, closed = {}, []
    for t in trades:
        mint, action = t["mint"], t["action"]
        if action == "BUY":
            open_[mint] = {"symbol": t["symbol"], "leader": t["leader"], "pnl": 0.0, "rugged": False}
            continue
        ep = open_.get(mint)
        if ep is None:
            continue
        ep["pnl"] += num(t["pnl_usd"]) or 0.0
        if action == "SELL":
            ep["rugged"] = t["reason"].startswith("rugged")
            closed.append(open_.pop(mint))
    return closed


def filter_check(decisions, quotes, since):
    """Median coin move since the decision: copied vs filtered out.

    Each coin counts once, so a leader buying it five times doesn't weigh it
    five times: from its first COPY if we ever copied it, else its first SKIP.
    """
    first = {}
    for d in decisions:
        t = parse_time(d["time"])
        then = num(d["price_then"])
        if t is None or t < since or not then:
            continue
        if d["verdict"] == "SKIP" and first_word(d["reason"]) in DESK_LIMITS:
            continue                      # desk limits say nothing about the filter
        prev = first.get(d["mint"])
        if prev is None or (d["verdict"] == "COPY" and prev["verdict"] != "COPY"):
            first[d["mint"]] = d
    copied, rejected = [], []
    for d in first.values():
        then = num(d["price_then"])
        q = quotes.get(d["mint"])
        now_price = q["price"] if q and (q.get("liquidity") or 0) >= config.RUG_LIQUIDITY_USD else 0.0
        move = (now_price / then - 1) * 100
        (copied if d["verdict"] == "COPY" else rejected).append(move)
    return copied, rejected


def build(state, trades, decisions, quotes, quotes_ok, at=None):
    at = paper.now() if at is None else at
    L = []
    stamp = datetime.datetime.fromtimestamp(at, datetime.timezone.utc)
    L.append("PAPER DESK REPORT  %s UTC" % stamp.strftime("%a %d %b %Y, %H:%M"))
    if not state:
        L += ["", "The desk hasn't run yet. Once it has, this page fills in.",
              "If this stays, check the WALLETS secret (see your Paper Desk guide)."]
        return "\n".join(L)

    started = state.get("started", at)
    start_day = datetime.datetime.fromtimestamp(started, datetime.timezone.utc)
    L.append("Day %d, running since %s. AI judge: %s." % (
        int((at - started) // 86400) + 1, start_day.strftime("%d %b %Y"),
        "ON" if config.AI_JUDGE else "off"))
    if not quotes_ok:
        L.append("(Couldn't fetch fresh prices this time; using the last known ones.)")

    # POT
    positions = state.get("positions") or {}
    cash = state.get("cash", 0.0)
    start = state.get("start_cash") or config.START_POT_USD
    rows = []
    for mint, p in positions.items():
        q = quotes.get(mint)
        price = q["price"] if q else (p.get("last_price") or 0.0)
        val = paper.exit_value(p["qty"], price, p.get("tax_pct", 0.0))
        rows.append((((price / p["entry"]) - 1) * 100 if p["entry"] else 0.0,
                     p["symbol"], p["leader"], val, (at - p["opened"]) / 3600))
    held = sum(r[3] for r in rows)
    pot = cash + held
    L += ["", "POT",
          "  %s -> %s  (%s, %s)" % (money(start), money(pot), pct((pot / start - 1) * 100),
                                    signed_money(pot - start)),
          "  cash %s + open positions %s (if sold now, after costs)" % (money(cash), money(held))]

    # TRADES
    closed = episodes(trades)
    bought = sum(1 for t in trades if t["action"] == "BUY")
    wins = [c for c in closed if c["pnl"] > 0]
    losses = [c for c in closed if c["pnl"] <= 0]
    L += ["", "TRADES",
          "  bought %d, closed %d (%d won, %d lost)" % (bought, len(closed), len(wins), len(losses))]
    if closed:
        best = max(closed, key=lambda c: c["pnl"])
        worst = min(closed, key=lambda c: c["pnl"])
        L.append("  win rate %.0f%%" % (100.0 * len(wins) / len(closed)))
        L.append("  best  %-10s %s" % (best["symbol"], signed_money(best["pnl"])))
        L.append("  worst %-10s %s" % (worst["symbol"], signed_money(worst["pnl"])))
    L.append("  rugged to zero: %d" % sum(1 for c in closed if c["rugged"]))
    costs = sum(num(t.get("costs_usd")) or 0.0 for t in trades)
    if trades:
        L.append("  costs paid %s (slippage, trading fees, network fees, coin taxes)" % money(costs))

    # OPEN
    L += ["", "OPEN (%d)" % len(rows)]
    if rows:
        L.append("  %-10s %-10s %8s %8s %6s" % ("coin", "leader", "vs entry", "value", "hours"))
        for change, sym, leader, val, hours in sorted(rows, reverse=True):
            L.append("  %-10s %-10s %8s %8s %6.0f" % (sym[:10], leader[:10], pct(change), money(val), hours))
    else:
        L.append("  nothing open")

    # DECISIONS
    copied = [d for d in decisions if d["verdict"] == "COPY"]
    skipped = [d for d in decisions if d["verdict"] != "COPY"]
    L += ["", "DECISIONS",
          "  %d decisions on %d coins: copied %d, skipped %d" % (
              len(decisions), len({d["mint"] for d in decisions}), len(copied), len(skipped))]
    counts = {}
    for d in skipped:
        w = first_word(d["reason"])
        counts[w] = counts.get(w, 0) + 1
    for w, n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:6]:
        L.append("  %4d  %s" % (n, w))

    # LEADERS
    leaders = {}                          # nickname -> [coins seen, coins copied, realised P&L]
    for d in decisions:
        r = leaders.setdefault(d["leader"], [set(), set(), 0.0])
        r[0].add(d["mint"])
        if d["verdict"] == "COPY":
            r[1].add(d["mint"])
    for t in trades:
        if t["pnl_usd"]:
            leaders.setdefault(t["leader"], [set(), set(), 0.0])[2] += num(t["pnl_usd"]) or 0.0
    L += ["", "LEADERS"]
    if leaders:
        L.append("  %-12s %5s %6s %9s" % ("nickname", "coins", "copied", "realised"))
        for nick, (seen, cop, pnl) in sorted(leaders.items(), key=lambda kv: -kv[1][2]):
            L.append("  %-12s %5d %6d %9s" % (nick[:12], len(seen), len(cop), signed_money(pnl)))
    else:
        L.append("  no leader buys seen yet")

    # FILTER CHECK
    L += ["", "FILTER CHECK (last 7 days)",
          "  How each coin moved since the leader bought it, whatever we did after."]
    if not quotes_ok:
        L.append("  Prices unavailable this run; back next time.")
    else:
        c, r = filter_check(decisions, quotes, at - 7 * 86400)
        for label, moves in (("copied", c), ("filtered out", r)):
            if moves:
                L.append("  %-13s %3d coins, median %s, %d went to ~zero" % (
                    label, len(moves), pct(statistics.median(moves)),
                    sum(1 for m in moves if m <= -90)))
            else:
                L.append("  %-13s   0 coins" % label)
        if c and r:
            if statistics.median(c) > statistics.median(r):
                L.append("  The filter is picking better than it rejects so far.")
            else:
                L.append("  The filter is NOT beating the coins it rejects yet.")
        else:
            L.append("  Not enough coins on both sides yet to tell.")

    L += ["", FOOTER]
    return "\n".join(L)


def load_state():
    if not os.path.exists(config.STATE_FILE):
        return None
    return paper.Paper().s


def main():
    state = load_state()
    trades = read_csv(config.TRADES_FILE)
    decisions = read_csv(config.DECISIONS_FILE)
    week_ago = paper.now() - 7 * 86400
    mints = list((state or {}).get("positions", {}))
    mints += [d["mint"] for d in decisions if (parse_time(d["time"]) or 0) >= week_ago]
    quotes, ok = {}, True
    if mints:
        try:
            quotes = net.prices(mints)
        except net.NetError as e:
            ok = False
            print("Couldn't fetch fresh prices: %s" % e)
    text = build(state, trades, decisions, quotes, ok)
    print(text)
    with open(config.REPORT_FILE, "w") as f:
        f.write("```\n" + text + "\n```\n\n"
                "Full logs: [trades.csv](trades.csv) · [decisions.csv](decisions.csv)\n")


if __name__ == "__main__":
    main()
