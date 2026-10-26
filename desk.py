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

NO_WALLETS = "No wallets yet. Add them to the WALLETS secret on GitHub (see your Paper Desk guide)."


def wallet_key(address):
    return hashlib.sha256(address.encode("utf-8")).hexdigest()[:16]


def sig_key(signature):
    """Signatures are stored hashed too: a block explorer turns a raw one into the wallet."""
    return hashlib.sha256(signature.encode("utf-8")).hexdigest()[:16]


class Desk:
    def __init__(self, wallets, state_path=None):
        self.wallets = dict(wallets)
        self.paper = paper.Paper(state_path)
        last_sig = self.paper.s["last_sig"]
        for key, value in last_sig.items():   # older state files hold raw signatures
            if len(value) != 16:
                last_sig[key] = sig_key(value)

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


def on_buy(desk, nick, ev, left_with=None):
    """left_with: the leader's balance of this coin after the last trade we've read."""
    p = desk.paper
    mint = ev["mint"]
    why = p.blocked(mint)
    if why in ("already_holding", "cooldown"):
        return
    age = paper.now() - ev["time"] if ev.get("time") else 0
    if not why and left_with is not None and left_with < 0.01 * ev.get("after", 0):
        why = "leader_already_sold"              # in and out before we saw either: we'd buy their exit
    elif not why and age > config.MAX_BUY_AGE_MINUTES * 60:
        why = "late %dmin" % (age // 60)          # the move we'd be copying is long gone
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
        p.positions[mint]["leader_held"] = ev.get("after", 0.0)
        desk.say("  paper BUY $%.0f of %s at $%s" % (config.TICKET_USD, symbol, paper.fmt(dx["price"], 6)))


def on_sell(desk, nick, ev, why=None):
    p = desk.paper
    mint = ev["mint"]
    pos = p.positions.get(mint)
    if not config.MIRROR_LEADER_SELLS or not pos or pos["leader"] != nick:
        return
    why = why or "%s sold %d%%" % (nick, round(ev["fraction"] * 100))
    dx = quote(mint)
    if not dx or (dx.get("liquidity") or 0) < config.RUG_LIQUIDITY_USD:
        # Selling at the last price seen would book a rug as if we got out in time.
        p.follow_out(mint, ev["fraction"])
        desk.say("  %s: %s -> no market right now, we sell at the first live price" % (pos["symbol"], why))
        return
    r = p.sell(mint, ev["fraction"], dx["price"], why)
    desk.say("  %s: %s -> we sell too (P&L $%+.2f)" % (pos["symbol"], why, r["pnl"]))


def unread(address, last):
    """Signatures newer than the one we stopped at, newest first, and whether we found it."""
    out, before = [], None
    while len(out) < config.SIG_LOOKBACK:
        page = net.signatures(address, config.MAX_TX_PER_CHECK, before=before)
        keys = [sig_key(entry["signature"]) for entry in page]
        if last in keys:
            return out + page[:keys.index(last)], True
        out += page
        if len(page) < config.MAX_TX_PER_CHECK:
            break                                # that's the wallet's whole history
        before = page[-1]["signature"]
    return out, False


def check_wallet(desk, nick, address):
    p = desk.paper
    key = wallet_key(address)
    last = p.s["last_sig"].get(key)
    if not last:
        sigs = net.signatures(address, 1)
        if sigs:
            p.s["last_sig"][key] = sig_key(sigs[0]["signature"])
            p.save()
        desk.say("watching %s from now on" % nick)
        return
    sigs, found = unread(address, last)
    if found:
        todo = sigs[::-1][:config.MAX_TX_PER_CHECK]      # oldest first; a backlog waits for next run
        if len(sigs) > len(todo):
            desk.say("  %s has %d new transactions: reading the oldest %d now, the rest next run"
                     % (nick, len(sigs), len(todo)))
    else:
        todo = sigs[:config.MAX_TX_PER_CHECK][::-1]      # lost our place: carry on from the newest
        if len(sigs) >= config.SIG_LOOKBACK:
            desk.say("  %s made %d+ transactions since last run (very busy, maybe a bot); "
                     "skipping to the newest %d" % (nick, len(sigs), len(todo)))

    # Read the whole batch before acting, so a buy the leader already sold isn't copied.
    reads = []
    for entry in todo:
        events = []
        if not entry.get("err"):
            try:
                tx = net.transaction(entry["signature"])
            except net.NetError as e:
                if not reads:
                    raise
                desk.say("  stopped reading %s early (%s); the rest next run" % (nick, e))
                break
            try:
                events = net.swaps(tx, address)
            except Exception as e:               # an odd transaction is skipped, not retried forever
                desk.say("  skipped one odd %s transaction: %s: %s" % (nick, type(e).__name__, e))
        reads.append((entry["signature"], events))
    left_with = {}
    for _, events in reads:
        for ev in events:
            left_with[ev["mint"]] = ev.get("after", 0.0)

    for sig, events in reads:
        for ev in events:
            try:
                if ev["side"] == "BUY":
                    on_buy(desk, nick, ev, left_with.get(ev["mint"]))
                else:
                    on_sell(desk, nick, ev)
            except Exception as e:
                desk.say("  skipped one odd %s transaction: %s: %s" % (nick, type(e).__name__, e))
        p.s["last_sig"][key] = sig_key(sig)
        p.save()                                 # after every transaction: a crash never replays a trade


def check_leaders_still_hold(desk, run_started):
    """Catch exits we never saw (an odd transaction, a wallet too busy to read in full):
    if a leader has dumped a coin we copied from them, we sell it too.

    Coins bought this run wait for the next: a node a few seconds behind could
    still show the leader's balance from before their buy.
    """
    if not config.MIRROR_LEADER_SELLS:
        return
    p = desk.paper
    for mint, pos in list(p.positions.items()):
        address = desk.wallets.get(pos["leader"])
        if not address or pos["opened"] >= int(run_started) or pos.get("exit_pending", 0.0) >= 0.999:
            continue
        try:
            held = net.token_balance(address, mint)
        except net.NetError:
            continue                             # can't tell this run
        if held <= 0.01 * pos.get("leader_held", 0.0):
            on_sell(desk, pos["leader"], {"mint": mint, "fraction": 1.0},
                    "%s no longer holds it" % pos["leader"])


def cycle(desk):
    p = desk.paper
    started = paper.now()
    for nick, address in desk.wallets.items():
        try:
            check_wallet(desk, nick, address)
        except net.NetError as e:
            desk.say("  couldn't check %s this run: %s" % (nick, e))
        except Exception as e:
            desk.say("  something went wrong checking %s: %s: %s" % (nick, type(e).__name__, e))
    try:
        check_leaders_still_hold(desk, started)
    except Exception as e:
        desk.say("  couldn't check what the leaders still hold: %s: %s" % (type(e).__name__, e))
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
