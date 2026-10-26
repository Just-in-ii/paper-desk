"""Find wallets worth following. For use on your own computer.

python3 finder.py                look at coins that ran (picked from DexScreener's listings)
python3 finder.py MINT [MINT]    look at these coins instead

For each coin it reads the first trades after launch and keeps the buyers
who have sold most of it since. For a pump.fun coin those trades are on its
launch account (the bonding curve), whose history is short; for any other
coin they are at the far end of the coin's own history, which only a young
coin lets us reach. Many launches are bundles (a few wallets buy everything
in the first second), leaving no early buyers worth the name, so it also
takes the wallets trading the coin right now. Then it drops the ones that
have gone quiet or trade like bots, and profiles the rest with scout.py.

Unlike everything else here it prints full wallet addresses: you need them to
follow a wallet. So it refuses to run on GitHub, where logs are public.
It is slow on the public Solana endpoint; put your own in rpc_url.txt first.
"""
import hashlib
import os
import sys

import config
import net
import paper
import scout

PAGE = 1000                  # signatures per page (the most Solana hands out at once)
ON_GITHUB = "finder.py prints wallet addresses, so it only runs on your own computer, not on GitHub."
PUMP_PROGRAM = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"

# ---------------------------------------------------------------------------
# Solana addresses: base58, and the program-derived address of a launch account
# ---------------------------------------------------------------------------

B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
P = 2 ** 255 - 19
D = -121665 * pow(121666, P - 2, P) % P


def b58decode(text):
    """32 raw bytes of an address; ValueError if it isn't one."""
    n = 0
    for c in text:
        n = n * 58 + B58.index(c)
    if n >> 256:
        raise ValueError("not a Solana address")
    return n.to_bytes(32, "big")


def b58encode(raw):
    n, out = int.from_bytes(raw, "big"), ""
    while n:
        n, r = divmod(n, 58)
        out = B58[r] + out
    return "1" * (len(raw) - len(raw.lstrip(b"\0"))) + out


def on_curve(raw):
    """True if 32 bytes decode to an ed25519 point (a program-derived address must not)."""
    y = (int.from_bytes(raw, "little") & ((1 << 255) - 1)) % P
    x2 = (y * y - 1) * pow(D * y * y + 1, P - 2, P) % P
    return x2 == 0 or pow(x2, (P - 1) // 2, P) == 1


def derived_address(seeds, program):
    pid = b58decode(program)
    for bump in range(255, -1, -1):
        h = hashlib.sha256(b"".join(seeds) + bytes([bump]) + pid + b"ProgramDerivedAddress").digest()
        if not on_curve(h):
            return b58encode(h)
    raise ValueError("no program address")


def pump_curve(mint):
    """The pump.fun bonding curve a coin launched on (if it launched there at all)."""
    return derived_address([b"bonding-curve", b58decode(mint)], PUMP_PROGRAM)


def launch_account(mint):
    """Where a coin's first trades are: its pump.fun bonding curve if it has one, else the coin."""
    try:
        curve = pump_curve(mint)
    except ValueError:
        return mint
    return curve if net.signatures(curve, 1) else mint


# ---------------------------------------------------------------------------
# Coins, and their early buyers
# ---------------------------------------------------------------------------

def reachable(info):
    """Rough guess at whether a coin's own first trade is within FINDER_MAX_PAGES of now."""
    days = max((info.get("age_hours") or 0) / 24.0, 1.0)
    return (info.get("txns_24h") or 0) * days <= config.FINDER_MAX_PAGES * PAGE


def pick_coins(most=None):
    """[(mint, symbol, launch account or None)] of listed coins that ran and held up, biggest first.

    None: its first trades are out of reach, so only the wallets trading it now are used.
    """
    market = net.prices(net.listed_coins())
    ran = [(info.get("market_cap") or 0, mint, info)
           for mint, info in market.items()
           if (info.get("market_cap") or 0) >= config.FINDER_MIN_MARKET_CAP_USD
           and info["liquidity"] >= config.FINDER_MIN_LIQUIDITY_USD
           and (info.get("age_hours") or 0) >= config.FINDER_MIN_AGE_HOURS]
    ran.sort(key=lambda r: -r[0])
    coins = []
    for _, mint, info in ran:
        if most is not None and len(coins) >= most:
            break
        try:
            source = launch_account(mint)
        except net.NetError:
            source = mint
        if source == mint and not reachable(info):
            source = None
        coins.append((mint, info.get("symbol") or mint[:6], source))
    return coins


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


def early_buyers(mint, symbol, source=None):
    """{wallet: coin balance right after its first buy} for buyers just after launch.

    source: the account whose history starts at launch (see launch_account).
    The first FINDER_SKIP_SECONDS are left out (snipers, the creator's own
    bundle), and so is the creator.
    """
    sigs = first_trades(source or mint)
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


def traders_now(mint, symbol):
    """Wallets that bought or sold the coin in its latest FINDER_RECENT_TX transactions."""
    sigs = [s for s in net.signatures(mint, config.FINDER_RECENT_TX) if not s.get("err")]
    seen, failed = [], 0
    for i, s in enumerate(sigs, 1):
        print("\r  %s: reading recent trade %d of %d" % (symbol, i, len(sigs)), end="", flush=True)
        try:
            tx = net.transaction(s["signature"])
            who = payer(tx)
            events = net.swaps(tx, who) if who else []
        except net.NetError:
            failed += 1
            continue
        except Exception:                        # an odd transaction is skipped
            continue
        if who not in seen and any(ev["mint"] == mint for ev in events):
            seen.append(who)
    print()
    if failed:
        print("  %s: %d recent trades couldn't be read" % (symbol, failed))
    return seen


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


def candidates(mint, symbol, source):
    """[(wallet, how we found it)] from one coin: early buyers who sold most of it since,
    plus, when there are few of those, the wallets trading it now."""
    sold = []
    if source:
        buyers = early_buyers(mint, symbol, source)
        sold = [w for w, bought in buyers.items() if bought > 0 and sold_most(w, mint, bought)]
        print("  %s: %d early buyers, %d sold most of it since" % (symbol, len(buyers), len(sold)))
    found = [(w, symbol + " early") for w in sold]
    if len(sold) < config.FINDER_MIN_EARLY_SELLERS:
        now = [w for w in traders_now(mint, symbol) if w not in sold]
        print("  %s: %d other wallets trading it now" % (symbol, len(now)))
        found += [(w, symbol) for w in now]
    return found


def find(coins):
    """Profile candidates from these coins. Returns [(wallet, profile, verdict)], best first."""
    following = set(config.load_wallets().values())
    seen = {}                                    # wallet -> where we found it
    for mint, symbol, source in coins:
        try:
            for w, how in candidates(mint, symbol, source):
                if w not in following:
                    seen.setdefault(w, []).append(how)
        except net.NetError as e:
            print("  %s: couldn't read its trades (%s)" % (symbol, e))

    # On several coins first, then early sellers before wallets that are just trading.
    ranked = sorted(seen, key=lambda w: (-len(seen[w]), -sum(h.endswith(" early") for h in seen[w])))
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
    print("\n%d candidates new to you. Dropped before profiling: %s"
          % (len(seen), ", ".join("%d %s" % (n, r) for r, n in sorted(dropped.items())) or "none"))

    sol_usd = scout.sol_price()
    results = []
    for i, w in enumerate(picked, 1):
        print("Profiling candidate %d of %d" % (i, len(picked)))
        try:
            p = scout.profile(w, config.SCOUT_TX, sol_usd=sol_usd)
        except net.NetError as e:
            print("  couldn't read it (%s)" % e)
            continue
        p["found_on"] = seen[w]
        results.append((w, p, scout.verdict(p)))
    results.sort(key=lambda r: (r[2] != "Looks followable.", -r[1]["usd_pnl"]))
    return results


def show(results):
    print("\n #  found on          trades/day  buys/day  win rate  P&L       hold    last trade     verdict")
    for i, (_, p, v) in enumerate(results, 1):
        win = "n/a" if p["win_rate"] is None else "%.0f%%" % p["win_rate"]
        hold = "n/a" if p["median_hold_min"] is None else "%.0fm" % p["median_hold_min"]
        print("%2d  %-16s  %10.1f  %8.1f  %8s  %+8.0f  %6s  %-13s  %s" % (
            i, ",".join(p["found_on"])[:16], p["per_day"], p["buys_per_day"], win, p["usd_pnl"],
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
        if argv:
            names = net.prices(argv)
            coins = [(m, (names.get(m) or {}).get("symbol") or m[:6], launch_account(m)) for m in argv]
        else:
            coins = pick_coins(config.FINDER_COINS)
    except net.NetError as e:
        print("Couldn't pick coins: %s" % e)
        sys.exit(1)
    if not coins:
        print("No listed coin has run and held up right now. Try again later, or name coins: "
              "python3 finder.py MINT")
        return
    print("Looking at: " + ", ".join(symbol for _, symbol, _ in coins))
    show(find(coins))


if __name__ == "__main__":
    main()
