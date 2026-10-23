"""Every setting for the paper desk lives here.

Change a number, save, and the desk uses it from the next run.
Nothing else in the code needs touching to change how it behaves.
"""
import os

HERE = os.path.dirname(os.path.abspath(__file__))

# ---------------------------------------------------------------------------
# 1. The pot (pretend money, in US dollars; $1 is roughly 75p)
# ---------------------------------------------------------------------------
START_POT_USD = 100.0        # pretend cash we start with
TICKET_USD = 10.0            # flat size of every buy; never sized up after a win

# ---------------------------------------------------------------------------
# 2. Desk limits (checked before we even look at the coin)
# ---------------------------------------------------------------------------
MAX_POSITIONS = 5            # most coins held at once
MAX_BUYS_PER_DAY = 10        # daily buy cap; resets each calendar day (UTC)
REBUY_COOLDOWN_HOURS = 24    # no re-entry into a coin we just left

# ---------------------------------------------------------------------------
# 3. Honest fills (so paper results don't flatter)
# ---------------------------------------------------------------------------
SLIPPAGE_PCT = 3.0           # buys fill 3% above the price, sells 3% below
FEE_USD = 0.10               # charged on every buy and every non-zero sell

# ---------------------------------------------------------------------------
# 4. Exits
# ---------------------------------------------------------------------------
STOP_LOSS_PCT = -50.0        # sell everything at or below -50% vs our entry
TAKE_PROFIT_PCT = 100.0      # the first time a coin reaches +100% vs entry...
TAKE_PROFIT_SELL = 0.5       # ...sell this share of it (only once per coin)
MIRROR_LEADER_SELLS = True   # when the leader sells X% of a coin, we sell X%
MAX_HOLD_HOURS = 168         # close anything still open after 7 days

# ---------------------------------------------------------------------------
# 5. Safety filter (Rugcheck + DexScreener). A coin failing any rule is skipped.
# ---------------------------------------------------------------------------
TOKEN = {
    "max_risk_score": 50,        # Rugcheck risk score 0-100 (higher = riskier)
    "max_top10_pct": 30,         # top-10 holders' share, pools excluded
    "min_lp_locked_pct": 90,     # liquidity locked; only used when Rugcheck knows it
    "max_insiders": 0,           # linked insider wallets allowed
    "min_liquidity_usd": 10000,  # minimum market liquidity to enter
}

# ---------------------------------------------------------------------------
# 6. Rug handling (rug = creators pull the money out, price goes to zero)
# ---------------------------------------------------------------------------
RUG_MISSES = 3               # runs in a row with no market before we write it off
RUG_LIQUIDITY_USD = 1000     # liquidity below this counts as "no market"

# ---------------------------------------------------------------------------
# 7. AI judge (Claude second opinion; off by default)
# ---------------------------------------------------------------------------
AI_JUDGE = False             # True = ask Claude COPY/SKIP on coins that passed the filter
AI_MODEL = "claude-haiku-5-5"  # cheap model
AI_MIN_CONFIDENCE = 0.65     # a COPY less sure than this is treated as SKIP

# ---------------------------------------------------------------------------
# 8. Plumbing
# ---------------------------------------------------------------------------
POLL_SECONDS = 60            # loop interval when running on your own computer
MAX_TX_PER_CHECK = 100       # transactions read per wallet per run

STATE_FILE = os.path.join(HERE, "state.json")
TRADES_FILE = os.path.join(HERE, "trades.csv")
DECISIONS_FILE = os.path.join(HERE, "decisions.csv")
REPORT_FILE = os.path.join(HERE, "REPORT.md")

PUBLIC_RPC = "https://api.mainnet-beta.solana.com"
RPC_FILE = os.path.join(HERE, "rpc_url.txt")
WALLETS_FILE = os.path.join(HERE, "wallets.txt")


def load_rpc_url():
    """RPC_URL env var, else rpc_url.txt, else the public endpoint."""
    env = os.environ.get("RPC_URL", "").strip()
    if env:
        return env
    if os.path.exists(RPC_FILE):
        with open(RPC_FILE) as f:
            text = f.read().strip()
        if text:
            return text
    return PUBLIC_RPC


RPC_URL = load_rpc_url()


def parse_wallets(text):
    """Turn 'nickname address' lines into {nickname: address}.

    Anything after # is ignored. The nickname may contain spaces. A line is
    only accepted if its last word looks like a Solana address (32-44 chars).
    """
    wallets = {}
    for line in (text or "").splitlines():
        line = line.split("#", 1)[0].strip()
        parts = line.split()
        if len(parts) < 2:
            continue
        address = parts[-1]
        if not 32 <= len(address) <= 44:
            continue
        nickname = " ".join(parts[:-1])
        wallets[nickname] = address
    return wallets


def load_wallets():
    """WALLETS env var, else wallets.txt, else nothing."""
    env = os.environ.get("WALLETS", "")
    if env.strip():
        return parse_wallets(env)
    if os.path.exists(WALLETS_FILE):
        with open(WALLETS_FILE) as f:
            return parse_wallets(f.read())
    return {}
