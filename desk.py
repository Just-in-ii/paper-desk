"""The paper desk's main loop.

python3 desk.py --once   one check (what GitHub runs every 15 minutes)
python3 desk.py          keep checking every POLL_SECONDS (on your own computer)

Console output only ever names wallets by nickname: GitHub logs on a
public repo are public.
"""
import hashlib
import sys
import time
import traceback

import config
import judge
import net
import paper

DECISION_COLUMNS = ["time", "leader", "symbol", "verdict", "reason", "price_then",
                    "liquidity_usd", "risk_score", "mint"]

NO_WALLETS = "No wallets yet. Add them to the WALLETS secret on GitHub (see README.md)."


def wallet_key(address):
    return hashlib.sha256(address.encode("utf-8")).hexdigest()[:16]


class Desk:
    def __init__(self, wallets, state_path=None):
        self.wallets = dict(wallets)
        self.paper = paper.Paper(state_path)

    def say(self, msg):
        """Print, with any wallet address swapped for its nickname, just in case."""
        msg = str(msg)
        for nick, addr in self.wallets.items():
            msg = msg.replace(addr, "<" + nick + ">")
        print(msg, flush=True)


def log_decision(leader, symbol, verdict, reason, mint, dx=None, rc=None):
    paper.append_csv(config.DECISIONS_FILE, DECISION_COLUMNS, {
        "time": paper.stamp(), "leader": leader, "symbol": symbol, "verdict": verdict,
        "reason": reason,
        "price_then": paper.fmt(dx["price"]) if dx else "",
        "liquidity_usd": "%.0f" % dx["liquidity"] if dx else "",
        "risk_score": "" if not rc or rc.get("risk_score") is None else rc["risk_score"],
        "mint": mint})


def quote(mint):
    """Live market for one coin, or None (no market or lookup failed)."""
    try:
        return net.prices([mint]).get(mint)
    except net.NetError:
        return None


def on_buy(desk, nick, ev):
    p = desk.paper
    mint = ev["mint"]
    why = p.blocked(mint)
    if why in ("already_holding", "cooldown"):
        return
    dx = quote(mint)
    if why:
        symbol = (dx or {}).get("symbol") or mint[:6]
        log_decision(nick, symbol, "SKIP", why, mint, dx)
        desk.say("  %s bought %s -> SKIP (%s)" % (nick, symbol, why))
        return
    try:
        rc = net.rugcheck(mint)
    except net.NetError:
        rc = None
    symbol = (rc or {}).get("symbol") or (dx or {}).get("symbol") or mint[:6]
    verdict, reason = judge.decide(nick, rc, dx)
    log_decision(nick, symbol, verdict, reason, mint, dx, rc)
    desk.say("  %s bought %s -> %s (%s)" % (nick, symbol, verdict, reason))
    if verdict == "COPY":
        p.buy(mint, dx["price"], symbol, nick, reason)
        desk.say("  paper BUY $%.0f of %s at $%s" % (config.TICKET_USD, symbol, paper.fmt(dx["price"], 6)))


def on_sell(desk, nick, ev):
    p = desk.paper
    pos = p.positions.get(ev["mint"])
    if not config.MIRROR_LEADER_SELLS or not pos or pos["leader"] != nick:
        return
    dx = quote(ev["mint"])
    price = dx["price"] if dx else None   # None -> last known price
    pct = round(ev["fraction"] * 100)
    r = p.sell(ev["mint"], ev["fraction"], price, "%s sold %d%%" % (nick, pct))
    if r:
        desk.say("  %s sold %d%% of %s -> we sell too (P&L $%+.2f)" % (nick, pct, pos["symbol"], r["pnl"]))


def check_wallet(desk, nick, address):
    p = desk.paper
    key = wallet_key(address)
    last = p.s["last_sig"].get(key)
    sigs = net.signatures(address, config.MAX_TX_PER_CHECK, until=last)
    if not last:
        if sigs:
            p.s["last_sig"][key] = sigs[0]["signature"]
            p.save()
        desk.say("watching %s from now on" % nick)
        return
    if len(sigs) >= config.MAX_TX_PER_CHECK:
        desk.say("  %s made %d+ transactions since last run (very busy, maybe a bot)"
                 % (nick, config.MAX_TX_PER_CHECK))
    for entry in reversed(sigs):                 # oldest first
        sig = entry["signature"]
        if not entry.get("err"):
            tx = net.transaction(sig)            # a NetError here stops this wallet; retried next run
            try:
                for ev in net.swaps(tx, address):
                    if ev["side"] == "BUY":
                        on_buy(desk, nick, ev)
                    else:
                        on_sell(desk, nick, ev)
            except net.NetError:
                raise
            except Exception as e:               # an odd transaction is skipped, not retried forever
                desk.say("  skipped one odd %s transaction: %s: %s" % (nick, type(e).__name__, e))
        p.s["last_sig"][key] = sig
        p.save()                                 # after every transaction: a crash never replays a trade


def cycle(desk):
    p = desk.paper
    for nick, address in desk.wallets.items():
        try:
            check_wallet(desk, nick, address)
        except net.NetError as e:
            desk.say("  couldn't check %s this run: %s" % (nick, e))
        except Exception as e:
            desk.say("  something went wrong checking %s: %s: %s" % (nick, type(e).__name__, e))
    quotes, ok = {}, True
    if p.positions:
        try:
            quotes = net.prices(list(p.positions))
        except net.NetError as e:
            ok = False
            desk.say("  couldn't fetch prices: %s" % e)
    for note in p.manage_exits(quotes, ok):
        desk.say("  " + note)
    cash, held = p.value()
    pot = cash + held
    start = p.s.get("start_cash") or config.START_POT_USD
    desk.say("pot $%.2f (%+.1f%%) | cash $%.2f | %d open"
             % (pot, (pot / start - 1) * 100, cash, len(p.positions)))
    p.save()


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    wallets = config.load_wallets()
    if not wallets:
        print(NO_WALLETS)
        sys.exit(0)         # not an error: GitHub shouldn't email a failure every 15 minutes
    desk = Desk(wallets)
    if "--once" in argv:
        cycle(desk)
        return
    print("Paper desk running. Checking every %ds. Press Ctrl+C to stop." % config.POLL_SECONDS)
    try:
        while True:
            try:
                cycle(desk)
            except Exception as e:
                desk.say("This round hit a problem and will try again: %s: %s" % (type(e).__name__, e))
                if "--debug" in argv:
                    traceback.print_exc()
            time.sleep(config.POLL_SECONDS)
    except KeyboardInterrupt:
        print("\nStopped. Your pretend pot is saved; run it again any time.")


if __name__ == "__main__":
    main()
