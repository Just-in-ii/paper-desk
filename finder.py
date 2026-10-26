"""Find wallets worth following. For use on your own computer.

python3 finder.py                look at coins that ran (picked from DexScreener's listings)
python3 finder.py MINT [MINT]    look at these coins instead

For each coin it reads the first trades after launch, keeps the buyers who
have sold most of it since, drops the ones that have gone quiet or trade like
bots, then profiles the most promising with scout.py.

Unlike everything else here it prints full wallet addresses: you need them to
follow a wallet. So it refuses to run on GitHub, where logs are public.
It is slow on the public Solana endpoint; put your own in rpc_url.txt first.
"""
import os
import sys

import config
import net
import paper
import scout

PAGE = 1000                  # signatures per page (the most Solana hands out at once)
ON_GITHUB = "finder.py prints wallet addresses, so it only runs on your own computer, not on GitHub."


def pick_coins():
    """[(mint, symbol)] of listed coins that ran and held up, biggest first."""
    market = net.prices(net.listed_coins())
    ran = [(info.get("market_cap") or 0, mint, info.get("symbol") or mint[:6])
           for mint, info in market.items()
           if (info.get("market_cap") or 0) >= config.FINDER_MIN_MARKET_CAP_USD
           and info["liquidity"] >= config.FINDER_MIN_LIQUIDITY_USD
           and (info.get("age_hours") or 0) >= config.FINDER_MIN_AGE_HOURS]
    ran.sort(reverse=True)
    return [(mint, symbol) for _, mint, symbol in ran]


def first_trades(mint):
    """A coin's oldest transactions, oldest first; None if its history is too deep to reach."""
    older, page, before = [], [], None
    for _ in range(config.FINDER_MAX_PAGES):
        older, page = page, net.signatures(mint, PAGE, before=before)
        if len(page) < PAGE:
            return (older + page)[::-1]          # two pages: the last one can be nearly empty
        before = page[-1]["signature"]
    return None


def payer(tx):
    keys = (((tx or {}).get("transaction") or {}).get("message") or {}).get("accountKeys") or []
    if not keys:
        return None
    return keys[0].get("pubkey") if isinstance(keys[0], dict) else keys[0]


def early_buyers(mint, symbol):
    """{wallet: coin balance right after its first buy} for buyers just after launch.

    The first FINDER_SKIP_SECONDS are left out (snipers, the creator's own
    bundle), and so is the creator.
    """
    sigs = first_trades(mint)
    if sigs is None:
        print("  %s: too much history to reach its first trades, skipped" % symbol)
        return {}
    sigs = [s for s in sigs if not s.get("err") and s.get("blockTime")]
    if not sigs:
        return {}
    start = sigs[0]["blockTime"]
    creator = payer(net.transaction(sigs[0]["signature"]))
    later = [s for s in sigs if s["blockTime"] - start >= config.FINDER_SKIP_SECONDS]
    buyers, failed = {}, 0
    for i, s in enumerate(later[:config.FINDER_EARLY_TX], 1):
        print("\r  %s: reading early trade %d of %d" % (symbol, i, min(len(later), config.FINDER_EARLY_TX)),
              end="", flush=True)
        try:
            tx = net.transaction(s["signature"])
            who = payer(tx)
            events = net.swaps(tx, who) if who else []
        except net.NetError:
            failed += 1
            continue
        except Exception:                        # an odd transaction is skipped
            continue
        for ev in events:
            if ev["side"] == "BUY" and ev["mint"] == mint and who != creator and who not in buyers:
                buyers[who] = ev.get("after", 0.0)
    print()
    if failed:
        print("  %s: %d early trades couldn't be read" % (symbol, failed))
    return buyers


def sold_most(wallet, mint, bought):
    """True if the wallet now holds under half of what it bought. A failed lookup counts as no."""
    try:
        return net.token_balance(wallet, mint) < 0.5 * bought
    except net.NetError:
        return False


def quick_look(wallet):
    """One cheap call before the slow profile. None = worth profiling, else why not."""
    try:
        sigs = net.signatures(wallet, config.SCOUT_TX)
    except net.NetError:
        return "couldn't read it"
    times = [s["blockTime"] for s in sigs if s.get("blockTime")]
    if not times:
        return "no transactions"
    now = paper.now()
    if now - max(times) > config.SCOUT_MAX_QUIET_DAYS * 86400:
        return "gone quiet"
    if len(sigs) >= config.SCOUT_TX and now - min(times) < 0.5 * 86400:
        return "bot-like: %d+ transactions in under 12 hours" % config.SCOUT_TX
    return None


def find(coins):
    """Profile the early sellers of these coins. Returns [(wallet, profile, verdict)], best first."""
    following = set(config.load_wallets().values())
    early = {}                                   # wallet -> symbols it was early on and sold
    found = took = 0
    for mint, symbol in coins:
        try:
            buyers = early_buyers(mint, symbol)
        except net.NetError as e:
            print("  %s: couldn't read its trades (%s)" % (symbol, e))
            continue
        sold = [w for w, bought in buyers.items() if bought > 0 and sold_most(w, mint, bought)]
        found += len(buyers)
        took += len(sold)
        print("  %s: %d early buyers, %d sold most of it since" % (symbol, len(buyers), len(sold)))
        for w in sold:
            if w not in following:
                early.setdefault(w, []).append(symbol)

    ranked = sorted(early, key=lambda w: -len(early[w]))   # early on several winners first
    picked, dropped = [], {}
    for w in ranked:
        if len(picked) >= config.FINDER_SCOUT_MAX:
            break
        why = quick_look(w)
        if why:
            reason = why.split(":")[0]
            dropped[reason] = dropped.get(reason, 0) + 1
        else:
            picked.append(w)
    print("\n%d early buyers, %d sold most since, %d new to you. Dropped before profiling: %s"
          % (found, took, len(early), ", ".join("%d %s" % (n, r) for r, n in sorted(dropped.items())) or "none"))

    sol_usd = scout.sol_price()
    results = []
    for i, w in enumerate(picked, 1):
        print("Profiling candidate %d of %d" % (i, len(picked)))
        try:
            p = scout.profile(w, config.SCOUT_TX, sol_usd=sol_usd)
        except net.NetError as e:
            print("  couldn't read it (%s)" % e)
            continue
        p["coins_early"] = early[w]
        results.append((w, p, scout.verdict(p)))
    results.sort(key=lambda r: (r[2] != "Looks followable.", -r[1]["usd_pnl"]))
    return results


def show(results):
    print("\n #  early on        trades/day  buys/day  win rate  P&L       hold    last trade     verdict")
    for i, (_, p, v) in enumerate(results, 1):
        win = "n/a" if p["win_rate"] is None else "%.0f%%" % p["win_rate"]
        hold = "n/a" if p["median_hold_min"] is None else "%.0fm" % p["median_hold_min"]
        print("%2d  %-14s  %10.1f  %8.1f  %8s  %+8.0f  %6s  %-13s  %s" % (
            i, ",".join(p["coins_early"])[:14], p["per_day"], p["buys_per_day"], win, p["usd_pnl"],
            hold, scout.ago(p["quiet_days"]), v))
    good = [w for w, _, v in results if v == "Looks followable."]
    if not good:
        print("\nNo candidate looks followable this time. Try again later, or name coins: "
              "python3 finder.py MINT")
        return
    print("\n%d look followable. To follow them, add these lines to the WALLETS secret on GitHub"
          " (rename them as you like):" % len(good))
    for i, w in enumerate(good, 1):
        print("  finder %d %s" % (i, w))
    print("Small samples: one good week can be luck. Drop any leader the report shows losing.")


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if os.environ.get("GITHUB_ACTIONS") == "true":
        print(ON_GITHUB)
        sys.exit(1)
    try:
        coins = [(m, m[:6]) for m in argv] if argv else pick_coins()[:config.FINDER_COINS]
    except net.NetError as e:
        print("Couldn't pick coins: %s" % e)
        sys.exit(1)
    if not coins:
        print("No listed coin has run and held up right now. Try again later, or name coins: "
              "python3 finder.py MINT")
        return
    print("Looking at: " + ", ".join(symbol for _, symbol in coins))
    show(find(coins))


if __name__ == "__main__":
    main()
