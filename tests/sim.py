"""Offline simulation of the paper desk. No network: every call is faked.

python3 tests/sim.py
"""
import ast
import contextlib
import glob
import http.client
import io
import json
import os
import re
import shutil
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import config  # noqa: E402
import desk    # noqa: E402
import finder  # noqa: E402
import judge   # noqa: E402
import net     # noqa: E402
import paper   # noqa: E402
import report  # noqa: E402
import scout   # noqa: E402

net.RPC_PAUSE = 0
net.BACKOFF = [0, 0, 0]
REAL_RPC = net.rpc

NICK = "fish one"
WALLET = "Fish1111WaLLetAddressXyzABCDEFGHJKLMNPQRSTU"   # 43 chars, fake
OTHER_NICK, OTHER = "whale", "WhaLe222WaLLetAddressXyzABCDEFGHJKLMNPQRSTU"
SCOUTED = "Sc0ut444WaLLetAddressXyzABCDEFGHJKLMNPQRSTU"
QUIET, SLOW = "Quiet555WaLLetAddressXyzABCDEFGHJKLMNPQRSTU", "SLoww666WaLLetAddressXyzABCDEFGHJKLMNPQRSTU"
CREATOR, SNIPER = "Creat777WaLLetAddressXyzABCDEFGHJKLMNPQRSTU", "Snipe888WaLLetAddressXyzABCDEFGHJKLMNPQRSTU"
EARLY, BOTTY = "EarLy999WaLLetAddressXyzABCDEFGHJKLMNPQRSTU", "Botty111WaLLetAddressXyzABCDEFGHJKLMNPQRSTV"
HOLDER = "HoLder22WaLLetAddressXyzABCDEFGHJKLMNPQRSTV"
POOL = "PooL333PooLAddressXyzABCDEFGHJKLMNPQRSTUVWX"
SOL, USDC = net.SOL_MINT, net.USDC_MINT


def mint(name):
    return (name + "Mint" + "x" * 44)[:44]


GOOD, BAD, CRASH, RUG, THIN = mint("GOOD"), mint("BAD"), mint("CRASH"), mint("RUG"), mint("THIN")

FAILS = []
PASSES = []


def check(name, ok, detail=""):
    (PASSES if ok else FAILS).append(name)
    print(("  ok   " if ok else "  FAIL ") + name + ("" if ok else "  -> %s" % detail))


# ---------------------------------------------------------------------------
# Fake world
# ---------------------------------------------------------------------------

class World:
    def __init__(self):
        self.sigs = {WALLET: [], OTHER: [], SCOUTED: []}   # oldest first
        self.txs = {}
        self.market = {}                         # mint -> (price, liquidity)
        self.rug = {}                            # mint -> raw rugcheck report
        self.held = {}                           # (wallet, mint) -> balance now
        self.lagging = False                     # balances read from a node that's behind
        self.dex_down = False
        self.rpc_down_for = set()
        self.n = 0
        self.clock = 1792483200.0   # Tue 20 Oct 2026, 08:00 UTC

    # -- transactions in real jsonParsed shape --
    def add_tx(self, wallet, sol_delta, token_changes, usdc_delta=0.0, err=None):
        """token_changes: {mint: (before, after)} for the wallet."""
        self.n += 1
        sig = "sig%04d" % self.n
        keys = [{"pubkey": wallet, "signer": True, "writable": True}, POOL, "11111111111111111111111111111111"]
        pre_bal = [5_000_000_000, 1_000_000_000_000, 1]
        post_bal = [pre_bal[0] + int(sol_delta * 1e9), pre_bal[1] - int(sol_delta * 1e9), 1]
        pre_t, post_t = [], []

        def bal(idx, m, owner, amt):
            return {"accountIndex": idx, "mint": m, "owner": owner, "programId": "Tokenkeg",
                    "uiTokenAmount": {"amount": str(int(amt * 1e6)), "decimals": 6,
                                      "uiAmount": amt, "uiAmountString": repr(float(amt))}}
        idx = 3
        for m, (before, after) in token_changes.items():
            if not err:
                self.held[(wallet, m)] = after
            if before:
                pre_t.append(bal(idx, m, wallet, before))
            if after:
                post_t.append(bal(idx, m, wallet, after))
            # The pool's balance moves the other way and must be ignored.
            pre_t.append(bal(idx + 1, m, POOL, 1_000_000.0))
            post_t.append(bal(idx + 1, m, POOL, 1_000_000.0 - (after - before)))
            idx += 2
        if usdc_delta:
            pre_t.append(bal(idx, USDC, wallet, 500.0))
            post_t.append(bal(idx, USDC, wallet, 500.0 + usdc_delta))
        tx = {"blockTime": int(self.clock), "slot": 1,
              "meta": {"err": err, "fee": 5000, "preBalances": pre_bal, "postBalances": post_bal,
                       "preTokenBalances": pre_t, "postTokenBalances": post_t},
              "transaction": {"message": {"accountKeys": keys}, "signatures": [sig]}}
        self.txs[sig] = tx
        self.sigs[wallet].append({"signature": sig, "err": err, "blockTime": int(self.clock)})
        return sig

    def rpc(self, method, params):
        if method == "getSignaturesForAddress":
            wallet, opts = params
            if wallet in self.rpc_down_for:
                raise net.NetError("Solana RPC getSignaturesForAddress: HTTP 429")
            newest_first = list(reversed(self.sigs[wallet]))
            if opts.get("before"):
                names = [s["signature"] for s in newest_first]
                newest_first = newest_first[names.index(opts["before"]) + 1:]
            return newest_first[:opts.get("limit", 1000)]
        if method == "getTransaction":
            return self.txs.get(params[0])
        if method == "getTokenAccountsByOwner":
            amt = 0.0 if self.lagging else self.held.get((params[0], params[1]["mint"]), 0.0)
            accounts = [{"pubkey": "TokenAcct", "account": {"data": {"parsed": {"info": {
                "mint": params[1]["mint"], "owner": params[0],
                "tokenAmount": {"uiAmount": amt, "uiAmountString": repr(float(amt))}}}}}}] if amt else []
            return {"context": {"slot": 1}, "value": accounts}
        raise AssertionError(method)

    def get_json(self, url):
        if url.startswith("https://api.dexscreener.com/"):
            if self.dex_down:
                raise net.NetError("DexScreener: HTTP 503")
            mints = url.rsplit("/", 1)[1].split(",")
            pairs = []
            for m in mints:
                if m not in self.market:
                    continue
                price, liq = self.market[m]
                sym = m[:4]
                # Decoy shallow pair first, with a silly price.
                pairs.append({"baseToken": {"address": m, "symbol": sym}, "priceUsd": "%.10f" % (price * 7),
                              "liquidity": {"usd": 300}, "pairCreatedAt": 1760000000000})
                pairs.append({"baseToken": {"address": m, "symbol": sym}, "priceUsd": "%.10f" % price,
                              "liquidity": {"usd": liq}, "fdv": 500000, "pairCreatedAt": 1760000000000,
                              "priceChange": {"h1": 12.5}, "volume": {"h1": 40000},
                              "txns": {"h1": {"buys": 120, "sells": 80}}})
                pairs.append({"baseToken": {"address": m, "symbol": sym}, "priceUsd": None,
                              "liquidity": {"usd": liq * 10}})      # no price: must be ignored
            return pairs
        if url.startswith("https://api.rugcheck.xyz/"):
            m = url.split("/tokens/")[1].split("/")[0]
            if m not in self.rug:
                raise net.NetError("Rugcheck: HTTP 404")
            return self.rug[m]
        raise AssertionError(url)


def rug_report(symbol, mint_auth=None, score=20, holders=None, rugged=False, lp=100.0):
    holders = holders or ([{"address": "PoolTokenAcct", "owner": POOL, "pct": 40.0, "insider": False}] +
                          [{"address": "h%d" % i, "owner": "o%d" % i, "pct": 2.0, "insider": False}
                           for i in range(12)])
    return {"tokenMeta": {"symbol": symbol}, "score": score * 10, "score_normalised": score,
            "rugged": rugged, "risks": [{"name": "Mutable metadata", "level": "warn"}],
            "token": {"mintAuthority": mint_auth, "freezeAuthority": None},
            "topHolders": holders, "totalHolders": 900, "graphInsidersDetected": 0,
            "insiderNetworks": None,
            "markets": [{"pubkey": "MarketPubkey", "liquidityA": POOL, "lp": {"lpLockedPct": lp}}],
            "knownAccounts": {POOL: {"name": "Raydium pool", "type": "AMM"}}}


@contextlib.contextmanager
def capture(sink):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        yield
    sink.append(buf.getvalue())


def rows(path):
    return report.read_csv(path)


# ---------------------------------------------------------------------------
# The run
# ---------------------------------------------------------------------------

def main():
    tmp = tempfile.mkdtemp(prefix="paperdesk-")
    console = []
    try:
        for name in ("STATE_FILE", "TRADES_FILE", "DECISIONS_FILE", "REPORT_FILE"):
            setattr(config, name, os.path.join(tmp, os.path.basename(getattr(config, name))))
        config.AI_JUDGE = False
        os.environ.pop("ANTHROPIC_API_KEY", None)

        w = World()
        net.rpc = w.rpc
        net.get_json = w.get_json
        paper.now = lambda: w.clock

        def cycle(d):
            with capture(console):
                desk.cycle(d)

        def tick(hours=0.25):
            w.clock += hours * 3600

        print("1. Baseline")
        w.add_tx(WALLET, -1.0, {mint("OLD"): (0, 500.0)})
        w.add_tx(WALLET, +1.2, {mint("OLD"): (500.0, 0)})
        d = desk.Desk({NICK: WALLET})
        cycle(d)
        check("first cycle only records 'watching from now'",
              "watching fish one from now on" in console[-1], console[-1])
        check("no decisions on baseline", not os.path.exists(config.DECISIONS_FILE))
        check("no positions on baseline", d.paper.positions == {})
        st = json.load(open(config.STATE_FILE))
        check("last_sig keyed by hash", desk.wallet_key(WALLET) in st["last_sig"])

        print("1b. Old state with a raw signature is migrated, nothing replayed")
        st["last_sig"][desk.wallet_key(WALLET)] = w.sigs[WALLET][-1]["signature"]
        with open(config.STATE_FILE, "w") as f:
            json.dump(st, f)
        d = desk.Desk({NICK: WALLET})
        check("raw signature hashed on load",
              d.paper.s["last_sig"][desk.wallet_key(WALLET)] == desk.sig_key(w.sigs[WALLET][-1]["signature"]))
        tick(); cycle(d)
        check("migrated state replays nothing", not os.path.exists(config.DECISIONS_FILE)
              and not os.path.exists(config.TRADES_FILE))

        print("2. Four buys in one cycle")
        for m, sym in ((GOOD, "GOOD"), (BAD, "BAD"), (CRASH, "CRASH"), (RUG, "RUG")):
            w.market[m] = (0.001, 50000.0)
            w.rug[m] = rug_report(sym, mint_auth=("SomeAuthority" if m == BAD else None))
            tick(0.01)
            w.add_tx(WALLET, -0.5, {m: (0, 10000.0)})
        tick()
        cycle(d)
        dec = rows(config.DECISIONS_FILE)
        verdicts = {r["symbol"]: (r["verdict"], r["reason"]) for r in dec}
        check("four decisions logged", len(dec) == 4, dec)
        check("GOOD, CRASH, RUG copied",
              all(verdicts.get(s, ("",))[0] == "COPY" for s in ("GOOD", "CRASH", "RUG")), verdicts)
        check("BAD skipped with authority_open", verdicts.get("BAD") == ("SKIP", "authority_open"), verdicts)
        pos = d.paper.positions
        check("three positions open", len(pos) == 3, list(pos))
        check("price from the deep pair, not the decoy",
              abs(pos[GOOD]["entry"] - 0.001 * 1.03) < 1e-12, pos[GOOD]["entry"])
        check("cash = 100 - 3 x 10.10", abs(d.paper.cash - (100 - 3 * 10.1)) < 1e-9, d.paper.cash)
        check("decision has price_then", dec[0]["price_then"] not in ("", None), dec[0])

        print("3. Moves")
        w.market[GOOD] = (0.0025, 60000.0)       # +150%
        w.market[CRASH] = (0.0004, 30000.0)      # -60%
        del w.market[RUG]                        # market vanishes
        tick()
        cycle(d)
        tr = rows(config.TRADES_FILE)
        good_sells = [t for t in tr if t["mint"] == GOOD and t["action"] != "BUY"]
        check("GOOD part sell of half", len(good_sells) == 1 and good_sells[0]["action"] == "PART SELL", good_sells)
        check("GOOD tp_done set", d.paper.positions[GOOD]["tp_done"] is True)
        crash = [t for t in tr if t["mint"] == CRASH and t["action"] == "SELL"]
        check("CRASH full stop-loss sell", len(crash) == 1 and crash[0]["reason"].startswith("stop loss"), crash)
        check("RUG misses = 1", d.paper.positions[RUG]["misses"] == 1, d.paper.positions.get(RUG))

        print("3b. Price fetch failure changes nothing")
        w.dex_down = True
        tick()
        cycle(d)
        check("misses unchanged when price fetch fails", d.paper.positions[RUG]["misses"] == 1)
        w.dex_down = False

        print("4. Two more cycles")
        tick(); cycle(d)
        tick(); cycle(d)
        tr = rows(config.TRADES_FILE)
        rug = [t for t in tr if t["mint"] == RUG and t["action"] == "SELL"]
        check("RUG written off at $0", len(rug) == 1 and rug[0]["reason"] == "rugged: market disappeared"
              and float(rug[0]["cash_flow_usd"]) == 0.0 and abs(float(rug[0]["pnl_usd"]) + 10.1) < 1e-6, rug)
        check("RUG no longer held", RUG not in d.paper.positions)

        print("5. Leader sells 100% of GOOD")
        w.add_tx(WALLET, +3.0, {GOOD: (10000.0, 0)})
        tick(); cycle(d)
        tr = rows(config.TRADES_FILE)
        last = [t for t in tr if t["mint"] == GOOD][-1]
        check("we sell the remainder", last["action"] == "SELL" and last["reason"] == "fish one sold 100%", last)
        check("GOOD closed", GOOD not in d.paper.positions)
        good_pnl = sum(float(t["pnl_usd"]) for t in tr if t["mint"] == GOOD and t["pnl_usd"])
        good_flow = sum(float(t["cash_flow_usd"]) for t in tr if t["mint"] == GOOD)
        check("partial sells keep P&L adding up", abs(good_pnl - good_flow) < 1e-6, (good_pnl, good_flow))

        print("6. Leader rebuys GOOD within 24h")
        n_dec = len(rows(config.DECISIONS_FILE))
        w.add_tx(WALLET, -0.5, {GOOD: (0, 9000.0)})
        tick(); cycle(d)
        check("rebuy ignored with no decision row", len(rows(config.DECISIONS_FILE)) == n_dec)
        check("no new GOOD position", GOOD not in d.paper.positions)

        print("6b. Edge cases")
        # USDC buy of a thin coin; Rugcheck missing for another.
        w.market[THIN] = (0.01, 4000.0)
        w.rug[THIN] = rug_report("THIN")
        w.add_tx(WALLET, -0.001, {THIN: (0, 100.0)}, usdc_delta=-25.0)
        nork = mint("NORK")
        w.market[nork] = (0.02, 90000.0)
        w.add_tx(WALLET, -0.4, {nork: (0, 100.0)})
        # A failed transaction must be ignored.
        w.add_tx(WALLET, -0.4, {mint("FAILED"): (0, 100.0)}, err={"InstructionError": [0, "x"]})
        tick(); cycle(d)
        dec = {r["symbol"]: r["reason"] for r in rows(config.DECISIONS_FILE)}
        check("USDC buy counted, thin liquidity skipped", dec.get("THIN") == "thin_liquidity $4000", dec)
        check("missing Rugcheck -> no_rugcheck_report", any(v == "no_rugcheck_report" for v in dec.values()), dec)
        check("failed tx ignored", not any(k.startswith("FAIL") for k in dec), dec)

        # One wallet's error never stops the others.
        w.rpc_down_for.add(OTHER)
        d2 = desk.Desk({OTHER_NICK: OTHER, NICK: WALLET})
        d2.paper = d.paper
        tick(); cycle(d2)
        check("one wallet's error doesn't stop others",
              "couldn't check whale" in console[-1] and "pot $" in console[-1], console[-1])

        # Sells: unknown price uses last_price; price 0 has no fee; fraction clamped.
        main_trades = config.TRADES_FILE
        config.TRADES_FILE = os.path.join(tmp, "unit_trades.csv")
        p = paper.Paper(os.path.join(tmp, "unit_state.json"))
        p.buy("M1", 1.0, "M1", "x", "t")
        p.positions["M1"]["last_price"] = 2.0
        r = p.sell("M1", 0.5, None, "t")
        check("unknown price uses last_price", abs(r["proceeds"] - (p.positions["M1"]["qty"] * 2 * 0.97 - 0.10)) < 1e-6, r)
        p.buy("M2", 1.0, "M2", "x", "t")
        r = p.sell("M2", 5.0, 0.0, "t")
        check("price 0 sell: no fee, fraction clamped, closed",
              r["proceeds"] == 0.0 and r["closed"] and abs(r["pnl"] + 10.1) < 1e-9, r)
        config.TRADES_FILE = main_trades

        print("6c. Scout values SOL and USDC trades")
        x, y, z = mint("SCX"), mint("SCY"), mint("SCZ")
        w.add_tx(SCOUTED, -0.001, {x: (0, 1000.0)}, usdc_delta=-100.0)   # buy with $100 USDC
        tick(0.5)
        w.add_tx(SCOUTED, 0.0, {x: (1000.0, 0)}, usdc_delta=+150.0)      # sell for $150 USDC: win
        w.add_tx(SCOUTED, -1.0, {y: (0, 500.0)})                         # buy with 1 SOL
        tick(0.5)
        w.add_tx(SCOUTED, +0.5, {y: (500.0, 0)})                         # sell for 0.5 SOL: loss
        w.add_tx(SCOUTED, -1.0, {z: (0, 10.0)})                          # buy with 1 SOL
        tick(0.5)
        w.add_tx(SCOUTED, 0.0, {z: (10.0, 0), mint("SCQ"): (0, 5.0)})    # swapped into another coin
        out = []
        with capture(out):
            sp = scout.profile(SCOUTED, 60, sol_usd=200.0)
        check("scout counts 3 round trips, values 2", sp["round_trips"] == 3 and sp["priced_trips"] == 2, sp)
        check("scout win rate counts USDC sales", sp["win_rate"] == 50.0, sp)
        check("scout P&L in dollars", abs(sp["usd_pnl"] - (50 - 100.2)) < 0.01, sp)
        check("scout median hold 30 min", abs(sp["median_hold_min"] - 30) < 0.01, sp)
        check("scout: buys a day count from the oldest transaction to now",
              abs(sp["buys_per_day"] - 3 / (1.5 / 24)) < 0.01 and sp["quiet_days"] == 0, sp)
        base = {"per_day": 10, "buys_per_day": 5, "quiet_days": 0.1, "round_trips": 8, "priced_trips": 8,
                "median_hold_min": 60, "win_rate": 60.0, "usd_pnl": 50.0}
        check("scout: a good wallet looks followable", scout.verdict(base) == "Looks followable.")
        check("scout: under 2 buys a day is too quiet to copy",
              scout.verdict(dict(base, buys_per_day=1.5)).startswith("Too quiet"))
        check("scout: no coin trades at all is gone quiet",
              scout.verdict(dict(base, quiet_days=None)).startswith("Gone quiet"))

        now_then = w.clock
        w.sigs[QUIET], w.sigs[SLOW] = [], []
        w.clock = now_then - 10 * 86400          # busy for an hour ten days ago, nothing since
        for i in range(6):
            q = mint("QT%d" % i)
            w.add_tx(QUIET, -1.0, {q: (0, 100.0)})
            tick(0.1)
            w.add_tx(QUIET, +2.0, {q: (100.0, 0)})
        w.clock = now_then - 4 * 86400           # one coin a day for four days, the last a day ago
        for i in range(4):
            q = mint("SL%d" % i)
            w.add_tx(SLOW, -1.0, {q: (0, 100.0)})
            tick(1)
            w.add_tx(SLOW, +2.0, {q: (100.0, 0)})
            tick(23 if i < 3 else 0)
        w.clock = now_then
        with capture(out):
            qp, slp = scout.profile(QUIET, 60, sol_usd=200.0), scout.profile(SLOW, 60, sol_usd=200.0)
        check("scout: busy ten days ago, silent since -> gone quiet, not a busy wallet",
              scout.verdict(qp).startswith("Gone quiet: its last coin trade was 10 days ago")
              and qp["per_day"] < 2, (qp, scout.verdict(qp)))
        check("scout: one coin a day -> too quiet to copy",
              scout.verdict(slp).startswith("Too quiet") and abs(slp["buys_per_day"] - 1.0) < 0.05,
              (slp, scout.verdict(slp)))
        check("scout: two coins is too little history, not a 'don't copy'",
              scout.verdict(dict(base, round_trips=2, priced_trips=2, win_rate=0.0)).startswith("Too little"))
        check("scout: too few valued coins to judge the win rate",
              scout.verdict(dict(base, priced_trips=3, win_rate=0.0)).startswith("Too few coins"))
        check("scout: wins often but loses money -> don't copy",
              scout.verdict(dict(base, usd_pnl=-20.0)).startswith("Don't copy"))

        print("6d. Real HTTP error handling (below the fakes)")
        import urllib.error
        import urllib.request
        real_urlopen, real_request = urllib.request.urlopen, net._request

        def fake_urlopen(code):
            def opener(req, timeout=None, context=None):
                raise urllib.error.HTTPError(req.full_url, code, "err", {}, io.BytesIO(b"not found"))
            return opener
        try:
            for code, expect in ((404, "HTTP 404"), (429, "gave up"), (503, "gave up")):
                urllib.request.urlopen = fake_urlopen(code)
                try:
                    real_request("https://example.invalid/x", label="Test")
                    got = "no error"
                except net.NetError as e:
                    got = str(e)
                except Exception as e:
                    got = "%s: %s" % (type(e).__name__, e)
                check("HTTP %d becomes a plain NetError" % code, expect in got, got)

            def cut_off(req, timeout=None, context=None):
                raise http.client.IncompleteRead(b"")
            urllib.request.urlopen = cut_off
            try:
                real_request("https://example.invalid/x", label="Test")
                got = "no error"
            except net.NetError as e:
                got = str(e)
            except Exception as e:
                got = "%s: %s" % (type(e).__name__, e)
            check("connection cut mid-reply is retried, then a NetError", "gave up" in got, got)

            saved_url = config.RPC_URL
            for bad, opener in (("mainnet.helius-rpc.com/?api-key=SECRET123", real_urlopen),
                                ("https://mainnet.helius-rpc.com/?api-key=SEC RET123", None)):
                def invalid(req, timeout=None, context=None):
                    raise http.client.InvalidURL("URL can't contain control characters. %r" % req.selector)
                urllib.request.urlopen = opener or invalid
                config.RPC_URL = bad
                try:
                    REAL_RPC("getHealth", [])
                    got = "no error"
                except net.NetError as e:
                    got = str(e)
                except Exception as e:
                    got = "%s: %s" % (type(e).__name__, e)
                check("bad RPC_URL: plain advice, key never shown (%s)" % bad[:12],
                      "RET123" not in got and "https://" in got, got)
            config.RPC_URL = saved_url
        finally:
            urllib.request.urlopen = real_urlopen

        print("7. Report")
        out = []
        with capture(out):
            report.main()
        md = open(config.REPORT_FILE).read()
        check("report written in a code fence", md.startswith("```\nPAPER DESK REPORT"), md[:40])
        check("logs link below fence", "Full logs: [trades.csv](trades.csv) · [decisions.csv](decisions.csv)" in md)
        check("pot shown", "POT" in md and "$100.00 ->" in md)
        check("1 win / 2 losses", "closed 3 (1 won, 2 lost)" in md, re.findall(r"closed.*", md))
        check("rug count 1", "rugged to zero: 1" in md)
        check("skip reasons grouped", "authority_open" in md and "thin_liquidity" in md and "no_rugcheck_report" in md)
        check("leader row", re.search(r"fish one\s+\d+\s+3", md) is not None, md)
        check("filter check lines", "FILTER CHECK (last 7 days)" in md and "copied" in md and "filtered out" in md)
        check("filter check verdict line", "The filter is" in md or "Not enough coins" in md)
        check("footer", report.FOOTER in md)
        print(md)

        # Filter check maths on hand-made rows.
        now = w.clock
        t = paper.stamp(now - 3600)
        fake = [{"time": t, "verdict": "COPY", "reason": "passed safety filter", "price_then": "1", "mint": "A"},
                {"time": t, "verdict": "SKIP", "reason": "authority_open", "price_then": "1", "mint": "B"},
                {"time": t, "verdict": "SKIP", "reason": "max_positions", "price_then": "1", "mint": "C"},
                {"time": t, "verdict": "SKIP", "reason": "rugged", "price_then": "1", "mint": "D"},
                {"time": paper.stamp(now - 8 * 86400), "verdict": "COPY", "reason": "x", "price_then": "1", "mint": "E"},
                {"time": t, "verdict": "SKIP", "reason": "authority_open", "price_then": "1", "mint": "B"},
                {"time": t, "verdict": "SKIP", "reason": "thin_liquidity $4000", "price_then": "1", "mint": "F"},
                {"time": t, "verdict": "COPY", "reason": "passed safety filter", "price_then": "2", "mint": "F"}]
        q = {"A": {"price": 2.0, "liquidity": 50000}, "B": {"price": 0.5, "liquidity": 500},
             "C": {"price": 9.0, "liquidity": 50000}, "E": {"price": 9.0, "liquidity": 50000},
             "F": {"price": 3.0, "liquidity": 50000}}
        c, rj = report.filter_check(fake, q, now - 7 * 86400)
        check("filter check: copied move, a coin copied after a skip counts as copied",
              sorted(c) == [50.0, 100.0], c)
        check("filter check: thin market counts as zero, desk limits left out, repeats once",
              sorted(rj) == [-100.0, -100.0], rj)

        print("8. AI judge")
        rc = net.summarise_rugcheck(rug_report("AIC"))
        dx = {"price": 1.0, "liquidity": 50000.0, "symbol": "AIC"}
        config.AI_JUDGE = True
        check("no key -> SKIP", judge.decide(NICK, rc, dx) ==
              ("SKIP", "AI_JUDGE is on but ANTHROPIC_API_KEY isn't set"))
        os.environ["ANTHROPIC_API_KEY"] = "test-key"
        sent = []

        def fake_ai(text):
            def post(url, body, headers=None, label=None):
                sent.append((url, body, headers))
                return {"content": [{"type": "text", "text": text}], "stop_reason": "end_turn"}
            return post

        net.post_json = fake_ai('{"verdict": "COPY", "confidence": 0.5, "why": "meh", "main_risk": "x"}')
        check("COPY at 0.5 -> low confidence SKIP", judge.decide(NICK, rc, dx) == ("SKIP", "ai_low_confidence 0.5"))
        net.post_json = fake_ai('Sure! Here you go:\n{"verdict": "COPY", "confidence": 0.8, '
                                '"why": "real crowd", "main_risk": "late"}\nHope that helps.')
        check("COPY at 0.8 with prose -> COPY", judge.decide(NICK, rc, dx) == ("COPY", "ai: real crowd"))
        net.post_json = fake_ai('{"verdict": "SKIP", "confidence": 0.9, "why": "exit liquidity", "main_risk": "x"}')
        check("AI SKIP -> ai_skip", judge.decide(NICK, rc, dx) == ("SKIP", "ai_skip: exit liquidity"))

        def boom(*a, **k):
            raise net.NetError("Anthropic API: HTTP 401")
        net.post_json = boom
        v = judge.decide(NICK, rc, dx)
        check("API error -> ai_failed", v[0] == "SKIP" and v[1].startswith("ai_failed: "), v)
        net.post_json = fake_ai("no json here")
        v = judge.decide(NICK, rc, dx)
        check("unparseable -> ai_failed", v[0] == "SKIP" and v[1].startswith("ai_failed: "), v)
        url, body, headers = sent[0]
        payload = body["messages"][0]["content"]
        check("AI payload has leader, safety, market only",
              set(json.loads(payload)) == {"leader", "token_safety", "market"}, payload)
        check("AI request: model/headers right, no sampling params",
              body["model"] == config.AI_MODEL and headers["anthropic-version"] == "2023-06-01"
              and headers["x-api-key"] == "test-key" and "temperature" not in body and body["max_tokens"] == 300, body)
        check("AI payload has no address", WALLET not in json.dumps(body))
        config.AI_JUDGE = False
        os.environ.pop("ANTHROPIC_API_KEY", None)

        print("9. Pool exclusion")
        check("top10_pct = 20.0", rc["top10_pct"] == 20.0, rc["top10_pct"])
        check("lp_locked_pct read from markets", rc["lp_locked_pct"] == 100.0, rc)
        r2 = rug_report("INS")
        r2["topHolders"][1]["insider"] = True
        r2["graphInsidersDetected"] = None
        r2["insiderNetworks"] = []
        check("insiders counted from top holders", net.summarise_rugcheck(r2)["insiders"] == 1)
        check("fact_kill order: risk score", judge.fact_kill(dict(rc, risk_score=70), dx) == "risk_score 70")
        check("fact_kill: danger", judge.fact_kill(dict(rc, danger=["Freeze"]), dx) == "danger: Freeze")
        check("fact_kill: top10", judge.fact_kill(dict(rc, top10_pct=45.5), dx) == "top10_hold 45.5%")
        check("fact_kill: lp", judge.fact_kill(dict(rc, lp_locked_pct=50), dx) == "lp_unlocked 50%")
        check("fact_kill: no price", judge.fact_kill(rc, None) == "no_price")
        check("fact_kill: unknown holder spread is a skip",
              judge.fact_kill(dict(rc, top10_pct=None), dx) == "no_holder_data")
        two_pools = [{"lp": {"lpLockedPct": 100, "baseUSD": 500, "quoteUSD": 500}},
                     {"lp": {"lpLockedPct": 0, "baseUSD": 20000, "quoteUSD": 20000}}]
        check("lp lock weighted by pool size: a small locked pool can't vouch for a big open one",
              net._lp_locked_pct(two_pools) == 2.44, net._lp_locked_pct(two_pools))
        check("lp lock without pool sizes: the least-locked pool decides",
              net._lp_locked_pct([{"lp": {"lpLockedPct": 100}}, {"lp": {"lpLockedPct": 40}}]) == 40)

        print("10. Wallet parsing")
        parsed = config.parse_wallets(
            "# my wallets\n"
            "fish one  %s   # found on GMGN\n"
            "big   whale %s\n"
            "shorty abc123\n"
            "\n   # indented comment\n"
            "%s\n" % (WALLET, OTHER, POOL))
        check("parsed nicknames with spaces, skipped junk",
              parsed == {"fish one": WALLET, "big whale": OTHER}, parsed)
        os.environ["WALLETS"] = ""
        saved_file = config.WALLETS_FILE
        config.WALLETS_FILE = os.path.join(tmp, "nope.txt")
        out = []
        code = None
        with capture(out):
            try:
                desk.main(["--once"])
            except SystemExit as e:
                code = e.code
        config.WALLETS_FILE = saved_file
        check("no wallets -> friendly exit", code == 0 and desk.NO_WALLETS in out[-1], out)
        os.environ["WALLETS"] = "env fish %s" % WALLET
        check("WALLETS env var wins", config.load_wallets() == {"env fish": WALLET})
        os.environ.pop("WALLETS")

        print("11. Privacy")
        leaks = []
        for path in (config.STATE_FILE, config.TRADES_FILE, config.DECISIONS_FILE, config.REPORT_FILE):
            text = open(path).read()
            for addr in (WALLET, OTHER):
                if addr in text:
                    leaks.append(os.path.basename(path))
        all_console = "".join(console)
        if WALLET in all_console or OTHER in all_console:
            leaks.append("console")
        check("no wallet address in any output file or console", not leaks, leaks)
        state_text = open(config.STATE_FILE).read()
        raw = [s for s in w.txs if '"%s"' % s in state_text]
        check("no raw transaction signature in state.json", not raw, raw)

        print("12. Exits, gaps and timing")
        sub = os.path.join(tmp, "s12")
        os.mkdir(sub)
        for name in ("STATE_FILE", "TRADES_FILE", "DECISIONS_FILE"):
            setattr(config, name, os.path.join(sub, os.path.basename(getattr(config, name))))
        w = World()
        net.rpc = w.rpc
        net.get_json = w.get_json
        d = desk.Desk({NICK: WALLET})
        key = desk.wallet_key(WALLET)
        w.add_tx(WALLET, -0.01, {})
        tick(); cycle(d)

        def copy(name):
            m = mint(name)
            w.market[m] = (0.001, 50000.0)
            w.rug[m] = rug_report(name)
            w.add_tx(WALLET, -0.5, {m: (0, 10000.0)})
            tick(); cycle(d)
            return m

        def exits(m):
            return [t for t in rows(config.TRADES_FILE) if t["mint"] == m and t["action"] != "BUY"]

        pend = copy("PEND")
        del w.market[pend]                       # pool pulled, then the leader sells
        w.add_tx(WALLET, +0.01, {pend: (10000.0, 0)})
        tick(); cycle(d)
        check("leader sells with no market: no sale at the old price",
              not exits(pend) and d.paper.positions[pend].get("exit_pending") == 1.0, exits(pend))
        w.market[pend] = (0.0004, 20000.0)
        tick(); cycle(d)
        sold = exits(pend)
        check("then we sell at the first live price",
              len(sold) == 1 and abs(float(sold[0]["price"]) - 0.0004 * 0.97) < 1e-12
              and pend not in d.paper.positions, sold)

        gone = copy("GONE")
        del w.market[gone]
        w.add_tx(WALLET, +0.01, {gone: (10000.0, 0)})
        for _ in range(config.RUG_MISSES):
            tick(); cycle(d)
        sold = exits(gone)
        check("market never comes back: written off at $0",
              len(sold) == 1 and sold[0]["reason"].startswith("rugged") and float(sold[0]["cash_flow_usd"]) == 0, sold)

        busy = copy("BUSY")
        w.add_tx(WALLET, +0.5, {busy: (10000.0, 0)})
        for _ in range(150):
            w.add_tx(WALLET, -0.0001, {})
        tick(); cycle(d)
        sold = exits(busy)
        check("busy wallet: a sell 150 transactions back is still read and mirrored",
              len(sold) == 1 and sold[0]["reason"] == "fish one sold 100%", sold)
        check("busy wallet: backlog noted", "reading the oldest 100 now, the rest next run" in console[-1], console[-1])
        tick(); cycle(d)
        check("backlog cleared on the next run",
              d.paper.s["last_sig"][key] == desk.sig_key(w.sigs[WALLET][-1]["signature"]))

        hide = copy("HIDE")
        w.held[(WALLET, hide)] = 50.0            # sold all but dust somewhere we never read
        tick(); cycle(d)
        sold = exits(hide)
        check("leader down to dust in a transaction we missed: we sell too",
              len(sold) == 1 and sold[0]["reason"] == "fish one no longer holds it", sold)

        w.lagging = True                         # this run's balances predate the leader's buy
        lag = copy("LAG")
        w.lagging = False
        check("a coin bought this run isn't sold off a lagging balance", lag in d.paper.positions)
        w.add_tx(WALLET, +0.5, {lag: (10000.0, 0)})
        tick(); cycle(d)

        rnd = mint("ROUND")
        w.market[rnd] = (0.001, 50000.0)
        w.rug[rnd] = rug_report("ROUND")
        w.add_tx(WALLET, -0.5, {rnd: (0, 10000.0)})
        w.add_tx(WALLET, +0.6, {rnd: (10000.0, 0)})
        tick(); cycle(d)
        dec = [r for r in rows(config.DECISIONS_FILE) if r["mint"] == rnd]
        check("leader bought and sold before we looked: not copied",
              rnd not in d.paper.positions and [r["reason"] for r in dec] == ["leader_already_sold"], dec)

        late = mint("LATE")
        w.market[late] = (0.001, 50000.0)
        w.rug[late] = rug_report("LATE")
        w.add_tx(WALLET, -0.5, {late: (0, 10000.0)})
        tick(2); cycle(d)
        dec = [r for r in rows(config.DECISIONS_FILE) if r["mint"] == late]
        check("a buy spotted 2 hours late isn't chased",
              late not in d.paper.positions and [r["reason"] for r in dec] == ["late 120min"], dec)

        saved_lookback, config.SIG_LOOKBACK = config.SIG_LOOKBACK, 200
        for _ in range(250):
            w.add_tx(WALLET, -0.0001, {})
        tick(); cycle(d)
        config.SIG_LOOKBACK = saved_lookback
        check("lost our place: carry on from the newest, and say so",
              "skipping to the newest 100" in console[-1]
              and d.paper.s["last_sig"][key] == desk.sig_key(w.sigs[WALLET][-1]["signature"]), console[-1])

        print("12b. Finder: early sellers of a coin that ran")
        find = mint("FIND")
        for x in (CREATOR, SNIPER, EARLY, BOTTY, HOLDER):
            w.sigs[x] = []
        w.sigs[find] = []

        def coin_tx(wallet, sol, before, after):
            w.add_tx(wallet, sol, {find: (before, after)})
            w.sigs[find].append(w.sigs[wallet][-1])

        coin_tx(CREATOR, -1.0, 0, 1000.0)        # launch, with the creator's own buy
        tick(5 / 3600.0)
        coin_tx(SNIPER, -1.0, 0, 1000.0)         # 5 seconds in: a sniper
        tick(55 / 3600.0)
        for x in (EARLY, BOTTY, HOLDER, WALLET, CREATOR):
            coin_tx(x, -0.5, 0, 1000.0)
        tick(0.5)
        for x in (EARLY, BOTTY, WALLET, SNIPER):
            coin_tx(x, +1.5, 1000.0, 0)          # sold out after it ran
        coin_tx(HOLDER, +0.15, 1000.0, 900.0)    # still holds most of it
        for i in range(5):                       # the early seller trades other coins well too
            q = mint("EA%d" % i)
            w.add_tx(EARLY, -0.5, {q: (0, 100.0)})
            tick(1)
            w.add_tx(EARLY, +0.8, {q: (100.0, 0)})
        for _ in range(config.SCOUT_TX):         # the other one fires off transactions like a bot
            w.add_tx(BOTTY, -0.0001, {})
        w.market[net.SOL_MINT] = (200.0, 1e9)

        small, old, busy = mint("SMALL"), mint("OLDIE"), mint("BUSYC")
        w.market[find], w.market[small] = (0.001, 50000.0), (0.001, 20000.0)
        w.market[old], w.market[busy] = (0.001, 50000.0), (0.001, 50000.0)
        listing = [{"chainId": "solana", "tokenAddress": m} for m in (find, small, old, busy, find)]
        listing.insert(1, {"chainId": "ethereum", "tokenAddress": "0xabc"})
        hours = {old: 24 * 30}                   # pair age in hours; the rest are a day old
        per_day = {busy: 500000}                 # trades in the last 24 hours; the rest 2,000

        def listed(url):
            if url == net.LISTING_URLS[1]:
                raise net.NetError("DexScreener: HTTP 503")
            if url in net.LISTING_URLS:
                return listing
            pairs = w.get_json(url)
            for pr in pairs or []:
                m = pr["baseToken"]["address"]
                pr["pairCreatedAt"] = (time.time() - hours.get(m, 24) * 3600) * 1000
                pr["txns"] = {"h24": {"buys": per_day.get(m, 2000) // 2, "sells": per_day.get(m, 2000) // 2}}
            return pairs
        net.get_json = listed
        check("finder picks listed Solana coins that ran, held up and whose launch it can still read",
              finder.pick_coins() == [(find, find[:4])], finder.pick_coins())

        saved_page, saved_pages = finder.PAGE, config.FINDER_MAX_PAGES
        finder.PAGE = 3
        oldest = finder.first_trades(find)
        check("finder reads back to a coin's first trade, oldest first",
              len(oldest) >= finder.PAGE and [s["signature"] for s in oldest]
              == [s["signature"] for s in w.sigs[find]][:len(oldest)], len(oldest))
        config.FINDER_MAX_PAGES = 2
        out = []
        with capture(out):
            deep = finder.early_buyers(find, "FIND")
        check("finder skips a coin whose history is too deep to reach", deep == {}
              and "too much history" in out[-1], out[-1])
        finder.PAGE, config.FINDER_MAX_PAGES = saved_page, saved_pages

        saved_env = {k: os.environ.pop(k, None) for k in ("WALLETS", "GITHUB_ACTIONS")}
        os.environ["WALLETS"] = "%s %s" % (NICK, WALLET)    # already followed
        with capture(out):
            found = finder.find([(find, "FIND")])
            finder.show(found)
        os.environ.pop("WALLETS")
        shown = out[-1]
        check("finder keeps only the early seller who still trades like a person",
              [x for x, _, _ in found] == [EARLY] and found[0][2] == "Looks followable.",
              [(x[:5], v) for x, _, v in found])
        check("finder leaves out the creator, the sniper, a holder, a bot and a wallet already followed",
              all(x not in shown for x in (CREATOR, SNIPER, HOLDER, BOTTY, WALLET))
              and "1 bot-like" in shown, shown)
        check("finder prints the followable wallet ready for the WALLETS secret",
              "  finder 1 %s\n" % EARLY in shown, shown)

        os.environ["GITHUB_ACTIONS"] = "true"
        calls = []
        net.get_json = lambda url: calls.append(url)
        refused = False
        with capture(out):
            try:
                finder.main([])
            except SystemExit:
                refused = True
        os.environ.pop("GITHUB_ACTIONS")
        os.environ.update({k: v for k, v in saved_env.items() if v is not None})
        net.get_json = w.get_json
        check("finder refuses to run on GitHub, where logs are public",
              refused and not calls and finder.ON_GITHUB in out[-1], out[-1])

        print("13. Syntax (Python 3.8)")
        bad = []
        for path in glob.glob(os.path.join(ROOT, "*.py")) + glob.glob(os.path.join(ROOT, "tests", "*.py")):
            try:
                ast.parse(open(path).read(), filename=path, feature_version=(3, 8))
            except SyntaxError as e:
                bad.append("%s: %s" % (os.path.basename(path), e))
        check("every file parses as Python 3.8", not bad, bad)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print("\n%d passed, %d failed" % (len(PASSES), len(FAILS)))
    if FAILS:
        print("Failed: " + ", ".join(FAILS))
        sys.exit(1)


if __name__ == "__main__":
    main()
