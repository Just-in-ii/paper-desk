"""Profile a wallet before you follow it. For use on your own computer.

python3 scout.py ADDRESS [N]     (N = how many recent transactions to read, default 60)
"""
import statistics
import sys

import net


def profile(address, n=60):
    sigs = [s for s in net.signatures(address, n) if not s.get("err")]
    events = []
    for i, s in enumerate(sigs, 1):
        print("\r  reading transaction %d of %d" % (i, len(sigs)), end="", flush=True)
        try:
            events += net.swaps(net.transaction(s["signature"]), address)
        except net.NetError:
            pass
    print()

    times = [e["time"] for e in events if e.get("time")]
    days = max((max(times) - min(times)) / 86400.0, 1 / 24.0) if times else 1.0
    coins = {}
    for e in sorted(events, key=lambda e: e.get("time") or 0):
        c = coins.setdefault(e["mint"], {"spent": 0.0, "got": 0.0, "first_buy": None, "first_sell": None})
        if e["side"] == "BUY":
            c["spent"] += -e["sol"]
            if c["first_buy"] is None:
                c["first_buy"] = e.get("time")
        else:
            c["got"] += max(0.0, e["sol"])
            if c["first_sell"] is None:
                c["first_sell"] = e.get("time")

    trips = [c for c in coins.values() if c["first_buy"] is not None and c["first_sell"] is not None]
    wins = sum(1 for c in trips if c["got"] > c["spent"])
    pnl = sum(c["got"] - c["spent"] for c in trips)
    holds = [(c["first_sell"] - c["first_buy"]) / 60.0 for c in trips
             if c["first_sell"] and c["first_buy"] and c["first_sell"] >= c["first_buy"]]
    return {
        "trades": len(events),
        "per_day": len(events) / days,
        "coins": len(coins),
        "round_trips": len(trips),
        "win_rate": 100.0 * wins / len(trips) if trips else None,
        "sol_pnl": pnl,
        "median_hold_min": statistics.median(holds) if holds else None,
    }


def verdict(p):
    if p["per_day"] > 100:
        return "Bot: more than 100 trades a day. Skip it."
    if p["median_hold_min"] is not None and p["median_hold_min"] < 5:
        return "Too fast to copy: it usually sells within 5 minutes."
    if p["win_rate"] is not None and p["win_rate"] < 45:
        return "Don't copy: it loses on most coins."
    if p["round_trips"] < 5:
        return "Too little history to judge. Try again with N = 200."
    return "Looks followable."


def main():
    if len(sys.argv) < 2:
        print("Usage: python3 scout.py ADDRESS [N]")
        sys.exit(1)
    address = sys.argv[1]
    n = int(sys.argv[2]) if len(sys.argv) > 2 else 60
    try:
        p = profile(address, n)
    except net.NetError as e:
        print("Couldn't read that wallet: %s" % e)
        sys.exit(1)
    print("Coin trades per day : %.1f" % p["per_day"])
    print("Different coins     : %d" % p["coins"])
    print("Round trips         : %d (coins both bought and sold)" % p["round_trips"])
    print("Win rate (by SOL)   : %s" % ("n/a" if p["win_rate"] is None else "%.0f%%" % p["win_rate"]))
    print("Rough SOL P&L       : %+.2f SOL" % p["sol_pnl"])
    print("Median hold         : %s" % ("n/a" if p["median_hold_min"] is None
                                         else "%.0f minutes" % p["median_hold_min"]))
    print()
    print("Verdict: " + verdict(p))
    print("(A small sample. One good week can be luck.)")


if __name__ == "__main__":
    main()
