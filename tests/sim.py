"""Offline simulation of the paper desk. No network: every call is faked.

python3 tests/sim.py
"""
import ast
import contextlib
import glob
import io
import json
import os
import re
import shutil
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import config  # noqa: E402
import desk    # noqa: E402
import judge   # noqa: E402
import net     # noqa: E402
import paper   # noqa: E402
import report  # noqa: E402

net.RPC_PAUSE = 0
net.BACKOFF = [0, 0, 0]

NICK = "fish one"
WALLET = "Fish1111WaLLetAddressXyzABCDEFGHJKLMNPQRSTU"   # 43 chars, fake
OTHER_NICK, OTHER = "whale", "WhaLe222WaLLetAddressXyzABCDEFGHJKLMNPQRSTU"
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
        self.sigs = {WALLET: [], OTHER: []}        # oldest first
        self.txs = {}
        self.market = {}                         # mint -> (price, liquidity)
        self.rug = {}                            # mint -> raw rugcheck report
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
            until = opts.get("until")
            out = []
            for s in newest_first:
                if s["signature"] == until:
                    break
                out.append(s)
            return out[:opts.get("limit", 1000)]
        if method == "getTransaction":
            return self.txs.get(params[0])
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
                {"time": paper.stamp(now - 8 * 86400), "verdict": "COPY", "reason": "x", "price_then": "1", "mint": "E"}]
        q = {"A": {"price": 2.0, "liquidity": 50000}, "B": {"price": 0.5, "liquidity": 500},
             "C": {"price": 9.0, "liquidity": 50000}, "E": {"price": 9.0, "liquidity": 50000}}
        c, rj = report.filter_check(fake, q, now - 7 * 86400)
        check("filter check: copied move", c == [100.0], c)
        check("filter check: thin market counts as zero, desk limits left out",
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

        print("12. Syntax (Python 3.8)")
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
