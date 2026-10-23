# Paper Desk

A pretend-money copy desk for Solana memecoins. It watches a few wallets you pick, checks every coin they buy for scam warning signs, and "buys" $10 of the ones that pass, from a pretend $100 pot. It runs on GitHub every 15 minutes, by itself, for free.

**👉 [Read the latest report (REPORT.md)](REPORT.md)** (it appears after the first run)

No real money, no wallet, no keys. Nothing is ever signed or sent. Prices are in US dollars ($1 ≈ 75p).

The question it's built to answer: **do the coins it copies do better than the coins it rejects?** The FILTER CHECK section at the bottom of the report tracks exactly that.

---

## Setup (all from your phone)

Use github.com in your phone's browser. The GitHub app can't edit secrets. If a page looks cramped, open your browser menu and tick **Desktop site**.

### 1. Create the repo
github.com → **+** (top right) → **New repository** → name it `paper-desk`, choose **Public**, leave everything else unticked → **Create repository**. Claude Code then pushes the code into it.

*Why public?* GitHub's run minutes are free on public repos. Your wallet picks stay hidden (see Privacy below).

### 2. Get a free Solana key
Sign up at **helius.dev** (free plan). On the dashboard, copy the **mainnet RPC URL**. It looks like `https://mainnet.helius-rpc.com/?api-key=abc123...`. Without it, the free public Solana server often blocks GitHub.

### 3. Pick 3 to 5 wallets to follow
Good places to look:
- **GMGN.ai**: Solana → Top traders / Smart money
- **Birdeye**: Top traders
- **DexScreener**: open a coin that did well recently → **Top Traders** tab

Skip wallets that trade hundreds of times a day (bots), flip coins within minutes (too fast to copy), or lose on most coins. Copy each wallet's address and give it a nickname.

### 4. Add the secrets
In your repo: **Settings** → **Secrets and variables** → **Actions** → **New repository secret**. Add:

| Name | Value |
|---|---|
| `RPC_URL` | the Helius URL from step 2 |
| `WALLETS` | one wallet per line: a nickname, a space, then the address |

Example `WALLETS` value (two lines; use the real addresses you copied):
```
fish one AbCdEfGhJkLmNoPqRsTuVwXyZ123456789abcdefghij
big whale ZyXwVuTsRqPoNmKjHgFeDcBa987654321zyxwvutsrqpo
```
Nicknames can have spaces. Anything after a `#` is ignored, so you can add notes: `fish one AbCd...ghij  # from GMGN`.

### 5. Start it
**Actions** tab → **paper-desk** (left) → **Run workflow** → **Run workflow**. If GitHub asks you to enable workflows first, say yes.

The first run only notes where each wallet is "now". It never copies old trades. From then on it runs every 15 minutes by itself (GitHub often starts a few minutes late; that's normal).

### 6. Check on it
Open **REPORT.md** in the repo. It refreshes about every 15 minutes. Give it **at least two weeks** before judging anything.

### 7. Change things
- **Rules**: ask Claude in plain English, e.g. "make the stop loss -40%". Every setting is in `config.py`.
- **Wallets**: edit the `WALLETS` secret (Settings → Secrets and variables → Actions → WALLETS → ✏️).
- **Pause**: Actions → paper-desk → **⋯** → **Disable workflow**. Enable it the same way.
- **Start over**: delete `state.json`, `trades.csv` and `decisions.csv`. The next run starts a fresh $100 pot.

### 8. Later: turn on the AI judge
Once you have two weeks of results with the plain filter:
1. Get an API key at **console.anthropic.com** and add a few dollars of credit. This is separate from your Claude Pro plan, which doesn't include API credit.
2. Add it as a secret named `ANTHROPIC_API_KEY`.
3. Ask Claude to set `AI_JUDGE = True` in `config.py`.

Then compare the next fortnight's report with the first. It uses Claude Haiku, the cheapest model; each check costs a tiny fraction of a cent.

### Keep it alive
GitHub may switch off scheduled runs on a public repo after 60 days with no activity, and emails you when it does. Click the link in that email to keep it running.

---

## How it decides

Every 15 minutes it reads each wallet's new transactions and spots coin buys and sells (in SOL or USDC).

**When a leader buys a coin,** three gates in order. Any failure is a SKIP, logged with its reason in `decisions.csv`.

1. **Desk limits.** Already holding it, or left it in the last 24 hours → ignored silently. At most 5 coins at once, at most 10 buys a day, and enough cash for a $10 ticket.
2. **Safety facts** (from Rugcheck and DexScreener). Skip if:
   - Rugcheck has no report on it (common for brand-new coins; that's expected)
   - it's flagged as already rugged (rug = the creators pull the money out and the price goes to zero)
   - the mint or freeze authority is still open (the creator can print more coins or freeze yours)
   - Rugcheck lists any "danger" risk
   - Rugcheck's risk score is missing or above 50 (out of 100)
   - the top 10 holders own more than 30% (not counting the trading pool)
   - less than 90% of the pool's liquidity is locked (when Rugcheck knows)
   - any linked insider wallets are detected
   - there's no live market, or less than $10,000 of liquidity
3. **AI judge** (off by default). Claude sees only the leader's nickname, the safety facts and the market numbers, and says COPY or SKIP with a confidence. Anything less sure than 65%, or any error, is a SKIP.

A lookup that fails is always a SKIP, never a COPY.

**A COPY** "buys" $10 at the current price plus 3% slippage, plus a $0.10 fee.

**Exits,** checked every run in this order:
1. **Rug:** no market (or under $1,000 liquidity) for 3 runs in a row → written off at $0.
2. **Stop loss:** down 50% or more vs our entry → sell everything.
3. **Take profit:** the first time it's up 100% → sell half (once only).
4. **Time limit:** still open after 7 days → sell everything.
5. **Mirror the leader:** when the wallet we copied sells some of a coin, we sell the same share. (Only for coins copied from that same wallet.)

Every sell fills 3% below the price, minus the $0.10 fee. Everything is logged in `trades.csv`.

---

## Privacy

- Wallet addresses live **only** in the `WALLETS` secret. Everything the desk writes (`state.json`, the CSVs, `REPORT.md`) and the public run logs use nicknames. State is keyed by a scrambled fingerprint (a hash) of each address.
- Coin names and the pretend P&L are public. That's fine.
- **Honest residual risk:** someone determined could look at which coin was bought and when, and work back to likely wallets. Full encryption was rejected because it would stop you reading the report on your phone.
- Your Helius URL contains your key; the desk never prints it.

---

## Honest caveats

- **Paper results flatter you.** A real copy happens after the leader's buy, usually at a worse price. Slippage and fees are included to narrow the gap, not close it.
- **Every 15 minutes is slow for memecoins.** A coin can crash well past -50% before the desk notices and sells.
- **It's public.** Anyone can see the code and the pretend P&L, but not your wallets.
- **Most people who copy-trade memecoins lose money.** If this ever goes real, use only a small amount you're happy to lose, in a separate wallet, and never paste a seed phrase or private key anywhere. UK note: every real sale is a Capital Gains Tax disposal, so keep the log.

---

## Running it on your own computer (optional)

You need Python 3.8 or newer (Macs have `python3`). Nothing to install.

1. Put your wallets in a file called `wallets.txt` (same format as the secret).
2. Optionally put your Helius URL in `rpc_url.txt`.
3. Run:

```
python3 desk.py          # checks every 60 seconds; Ctrl+C to stop
python3 desk.py --once   # one check
python3 report.py        # print the report and update REPORT.md
python3 scout.py ADDRESS        # profile a wallet before following it
python3 scout.py ADDRESS 200    # ...using more history
python3 tests/sim.py     # offline self-test with fake data
```

`wallets.txt` and `rpc_url.txt` are never uploaded (they're in `.gitignore`).

---

## Troubleshooting

- **"No wallets yet"**: the `WALLETS` secret is missing or empty, or no line has a valid address (32 to 44 characters). Check step 4.
- **HTTP 429 / "Too many requests"**: the free public Solana server is rate-limiting. Add the `RPC_URL` secret from Helius (step 2).
- **Certificate error** (on your own computer only): run `pip3 install certifi`, then try again.
- **"very busy, maybe a bot"**: that wallet made more than 100 transactions between runs. Real traders rarely do. Consider swapping it out (`scout.py` helps).
- **"couldn't check ..." or "couldn't fetch prices" now and then**: normal. Free services hiccup. The desk skips and tries again next run. It only matters if it happens every run.
- **Lots of `no_rugcheck_report` skips**: expected. Brand-new coins often aren't indexed by Rugcheck yet.

---

## Files

| File | What it is |
|---|---|
| `config.py` | every setting |
| `desk.py` | the main loop |
| `net.py` | talks to Solana, Rugcheck and DexScreener |
| `judge.py` | the safety filter and the optional AI judge |
| `paper.py` | the pretend portfolio |
| `report.py` | builds `REPORT.md` |
| `scout.py` | wallet profiler |
| `tests/sim.py` | offline self-test |
| `state.json`, `trades.csv`, `decisions.csv`, `REPORT.md` | written by the desk as it runs |
