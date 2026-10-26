# Paper desk

Paper-trading copy desk. It follows Solana wallets (the WALLETS secret), copies their
memecoin buys with pretend money once a coin passes a safety filter, mirrors their exits,
and writes REPORT.md. GitHub Actions runs it every 15 minutes (.github/workflows/desk.yml)
and commits state.json, trades.csv, decisions.csv and REPORT.md back to main.

## Goal

Make more money and lose as little as possible. Keep paper fills honest (slippage, fees,
rugs at $0, never selling at a stale price): flattering paper results would lead to real
losses later. A failed lookup means SKIP, never COPY.

## Files

- config.py: every setting
- desk.py: main loop (read leader wallets, copy buys, mirror sells, check leaders still hold)
- paper.py: pretend portfolio, fills and exits
- net.py: Solana RPC, swap detection, Rugcheck, DexScreener
- judge.py: COPY/SKIP (hard safety rules, then an optional Claude judge, off by default)
- report.py: builds REPORT.md
- scout.py: profiles a wallet before following it (run locally)
- tests/sim.py: offline simulation, the only test suite

## Working on it

- Test with `python3 tests/sim.py` (no network). It must end "N passed, 0 failed".
  Add a check for every behaviour change.
- Standard library only, Python 3.8 syntax (the suite checks).
- The repo is public, and the owner is fine with that. Still, never write a wallet
  address or a raw transaction signature to any file or console line (state stores both
  hashed), and never print RPC_URL: it holds an API key.
- Don't hand-edit state.json, trades.csv, decisions.csv or REPORT.md: the workflow owns
  them and commits every run.
