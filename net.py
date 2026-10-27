"""All network calls: Solana RPC, swap detection, Rugcheck and DexScreener.

Every call sends a User-Agent, times out after 20 seconds and retries
busy/broken answers (HTTP 429, 5xx, network errors) with a growing wait.
Anything that still fails raises NetError, which the desk treats as SKIP.
"""
import http.client
import json
import re
import ssl
import time
import urllib.error
import urllib.request

import config

USER_AGENT = "paper-desk/1.0"
TIMEOUT = 20
ATTEMPTS = 4                 # first try plus up to 3 retries
BACKOFF = [2, 4, 8]          # seconds to wait before each retry
RPC_PAUSE = 0.2              # be gentle on the free Helius tier

SOL_MINT = "So11111111111111111111111111111111111111112"
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"

RUGCHECK_URL = "https://api.rugcheck.xyz/v1/tokens/{}/report"
DEXSCREENER_URL = "https://api.dexscreener.com/tokens/v1/solana/{}"
LISTING_URLS = ["https://api.dexscreener.com/token-boosts/top/v1",
                "https://api.dexscreener.com/token-boosts/latest/v1",
                "https://api.dexscreener.com/token-profiles/latest/v1"]

CERT_HELP = ("Your computer couldn't check the website's security certificate. "
             "See Troubleshooting in your Paper Desk guide (usually fixed with: pip3 install certifi).")
BAD_URL = "%s: that isn't a valid web address (it should start with https:// and have no spaces)"

_ADDRESS_RE = re.compile(r"[1-9A-HJ-NP-Za-km-z]{32,44}")


class NetError(Exception):
    """A lookup failed after retries. Always means SKIP, never COPY."""


def _ssl_context():
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()


_CTX = _ssl_context()


def _request(url, body=None, headers=None, label=None):
    """GET (no body) or POST (JSON body) and return parsed JSON."""
    label = label or url
    data = None
    hdrs = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        hdrs["Content-Type"] = "application/json"
    hdrs.update(headers or {})
    # A malformed address goes in no error text: the RPC one holds a key.
    try:
        req = urllib.request.Request(url, data=data, headers=hdrs)
    except ValueError:
        raise NetError(BAD_URL % label)
    last = "unknown error"
    for attempt in range(ATTEMPTS):
        if attempt:
            time.sleep(BACKOFF[min(attempt - 1, len(BACKOFF) - 1)])
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT, context=_CTX) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except http.client.InvalidURL:
            raise NetError(BAD_URL % label)
        except urllib.error.HTTPError as e:
            if e.code == 429 or e.code >= 500:
                last = "HTTP %d" % e.code
                continue
            detail = ""
            try:
                detail = e.read().decode("utf-8", "replace")[:200]
            except Exception:
                pass
            raise NetError(("%s: HTTP %d %s" % (label, e.code, detail)).strip())
        except (urllib.error.URLError, OSError, ValueError, http.client.HTTPException) as e:
            text = str(getattr(e, "reason", e))
            if "CERTIFICATE_VERIFY_FAILED" in text:
                raise NetError(CERT_HELP)
            last = text
            continue
    raise NetError("%s: gave up after %d tries (%s)" % (label, ATTEMPTS, last))


def get_json(url):
    return _request(url)


def post_json(url, body, headers=None, label=None):
    return _request(url, body=body, headers=headers, label=label)


def rpc(method, params):
    """One Solana JSON-RPC call. Never puts the RPC URL (it holds a key) in errors."""
    body = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    try:
        reply = post_json(config.RPC_URL, body, label="Solana RPC " + method)
    except NetError as e:
        raise NetError(str(e).replace(config.RPC_URL, "<RPC_URL>"))
    finally:
        time.sleep(RPC_PAUSE)
    if isinstance(reply, dict) and reply.get("error"):
        err = reply["error"]
        msg = err.get("message", err) if isinstance(err, dict) else err
        # RPC errors can echo the address we asked about; hide anything address-shaped.
        msg = _ADDRESS_RE.sub("<hidden>", str(msg))
        raise NetError("Solana RPC %s: %s" % (method, msg))
    return reply.get("result") if isinstance(reply, dict) else None


# ---------------------------------------------------------------------------
# Solana: signatures, transactions and swap detection
# ---------------------------------------------------------------------------

def signatures(wallet, limit, before=None):
    """Newest first, starting just older than `before` if given.

    Failed transactions are included, with "err" set.
    """
    opts = {"limit": limit}
    if before:
        opts["before"] = before
    return rpc("getSignaturesForAddress", [wallet, opts]) or []


def transaction(sig):
    return rpc("getTransaction", [sig, {"encoding": "jsonParsed",
                                        "maxSupportedTransactionVersion": 1}])


def _amount(entry):
    ui = entry.get("uiTokenAmount") or {}
    s = ui.get("uiAmountString")
    if s not in (None, ""):
        try:
            return float(s)
        except ValueError:
            pass
    return float(ui.get("uiAmount") or 0)


def _owned(balances, wallet):
    out = {}
    for b in balances or []:
        if b.get("owner") != wallet:
            continue
        mint = b.get("mint")
        out[mint] = out.get(mint, 0.0) + _amount(b)
    return out


def swaps(tx, wallet):
    """Turn one jsonParsed transaction into BUY/SELL events for this wallet.

    Each event: {"side", "mint", "fraction" (sells), "sol" (SOL change), "usdc",
    "after" (the wallet's balance of the coin once the trade is done), "time"}.
    """
    if not tx:
        return []
    meta = tx.get("meta") or {}
    if meta.get("err"):
        return []
    keys = (((tx.get("transaction") or {}).get("message") or {}).get("accountKeys")) or []
    keys = [k.get("pubkey") if isinstance(k, dict) else k for k in keys]
    sol = 0.0
    if wallet in keys:
        i = keys.index(wallet)
        pre, post = meta.get("preBalances") or [], meta.get("postBalances") or []
        if i < len(pre) and i < len(post):
            sol = (post[i] - pre[i]) / 1e9

    before = _owned(meta.get("preTokenBalances"), wallet)
    after = _owned(meta.get("postTokenBalances"), wallet)
    change = {}
    for mint in set(before) | set(after):
        change[mint] = after.get(mint, 0.0) - before.get(mint, 0.0)

    sol += change.pop(SOL_MINT, 0.0)
    usdc = change.pop(USDC_MINT, 0.0)
    spent_quote = sol < -0.005 or usdc < -1

    when = tx.get("blockTime")
    events = []
    for mint, delta in change.items():
        start = before.get(mint, 0.0)
        if delta > 0 and spent_quote:
            events.append({"side": "BUY", "mint": mint, "fraction": 0.0,
                           "sol": sol, "usdc": usdc, "after": after.get(mint, 0.0), "time": when})
        elif delta < 0 and start > 0:
            # A transfer-out also counts: either way, the leader has left.
            events.append({"side": "SELL", "mint": mint,
                           "fraction": min(1.0, -delta / start),
                           "sol": sol, "usdc": usdc, "after": after.get(mint, 0.0), "time": when})
    return events


def token_balance(wallet, mint):
    """How much of a coin a wallet holds right now, across all its token accounts."""
    reply = rpc("getTokenAccountsByOwner", [wallet, {"mint": mint}, {"encoding": "jsonParsed"}]) or {}
    total = 0.0
    for acct in reply.get("value") or []:
        info = ((((acct.get("account") or {}).get("data") or {}).get("parsed") or {}).get("info")) or {}
        total += _amount({"uiTokenAmount": info.get("tokenAmount")})
    return total


# ---------------------------------------------------------------------------
# Rugcheck
# ---------------------------------------------------------------------------

def _strings(obj, out):
    if isinstance(obj, str):
        out.add(obj)
    elif isinstance(obj, dict):
        for v in obj.values():
            _strings(v, out)
    elif isinstance(obj, list):
        for v in obj:
            _strings(v, out)
    return out


def _pool_accounts(r):
    pools = set()
    for m in r.get("markets") or []:
        _strings(m, pools)
    known = r.get("knownAccounts") or {}
    items = known.items() if isinstance(known, dict) else [
        (k.get("address") or k.get("pubkey"), k) for k in known if isinstance(k, dict)]
    for addr, info in items:
        kind = str((info or {}).get("type", "")).lower()
        if addr and ("amm" in kind or "pool" in kind):
            pools.add(addr)
    return pools


def _is_int(x):
    return isinstance(x, int) and not isinstance(x, bool)


def _lp_locked_pct(markets):
    """Share of pool money that's locked, each pool weighted by its size.

    A small locked pool can't vouch for a big unlocked one. Without pool
    sizes, the least-locked pool decides.
    """
    pools = []
    for m in markets:
        lp = m.get("lp") if isinstance(m, dict) else None
        if not isinstance(lp, dict) or _num(lp.get("lpLockedPct")) is None:
            continue
        size = (_num(lp.get("baseUSD")) or 0.0) + (_num(lp.get("quoteUSD")) or 0.0)
        pools.append((_num(lp["lpLockedPct"]), size))
    if not pools:
        return None
    total = sum(size for _, size in pools)
    if total > 0:
        return round(sum(pct * size for pct, size in pools) / total, 2)
    return round(min(pct for pct, _ in pools), 2)


def summarise_rugcheck(r):
    """Shrink a full Rugcheck report to the facts the judge uses."""
    token = r.get("token") or {}
    risks = r.get("risks") or []
    pools = _pool_accounts(r)
    holders = [h for h in (r.get("topHolders") or [])
               if h.get("owner") not in pools and h.get("address") not in pools][:10]
    top10 = round(sum(float(h.get("pct") or 0) for h in holders), 2) if holders else None

    lp_locked = _lp_locked_pct(r.get("markets") or [])

    g = r.get("graphInsidersDetected")
    insiders = g if _is_int(g) else len(r.get("insiderNetworks") or [])
    insiders = max(insiders, sum(1 for h in holders if h.get("insider")))

    def auth(name):
        return token[name] if name in token else r.get(name)

    score = r.get("score_normalised")
    return {
        "symbol": (r.get("tokenMeta") or {}).get("symbol"),
        "risk_score": score,
        "rugged": bool(r.get("rugged")),
        "danger": [x.get("name") for x in risks if x.get("level") == "danger"],
        "warn": [x.get("name") for x in risks if x.get("level") == "warn"],
        "mint_authority": auth("mintAuthority"),
        "freeze_authority": auth("freezeAuthority"),
        "top10_pct": top10,
        "lp_locked_pct": lp_locked,
        "insiders": insiders,
        "holders": r.get("totalHolders"),
        "transfer_fee_pct": _num((r.get("transferFee") or {}).get("pct")) or 0.0,
    }


def rugcheck(mint):
    r = get_json(RUGCHECK_URL.format(mint))
    if not isinstance(r, dict):
        raise NetError("Rugcheck: unexpected reply for " + mint)
    return summarise_rugcheck(r)


# ---------------------------------------------------------------------------
# DexScreener
# ---------------------------------------------------------------------------

def _num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def listed_coins():
    """Solana coins DexScreener is showing off now (boosted, or a new profile).

    Boosts are paid for, so this is a place to look, not a recommendation.
    """
    mints, failed = [], None
    for url in LISTING_URLS:
        try:
            entries = get_json(url)
        except NetError as e:
            failed = e
            continue
        for e in entries if isinstance(entries, list) else []:
            if isinstance(e, dict) and e.get("chainId") == "solana" and e.get("tokenAddress"):
                if e["tokenAddress"] not in mints:
                    mints.append(e["tokenAddress"])
    if failed and not mints:
        raise failed
    return mints


def prices(mints):
    """{mint: market info} using each mint's deepest pair. Missing = no live market."""
    mints = [m for m in dict.fromkeys(mints) if m]
    out = {}
    for i in range(0, len(mints), 30):
        chunk = mints[i:i + 30]
        pairs = get_json(DEXSCREENER_URL.format(",".join(chunk)))
        if isinstance(pairs, dict):           # older shape: {"pairs": [...]}
            pairs = pairs.get("pairs") or []
        best = {}
        for p in pairs or []:
            mint = (p.get("baseToken") or {}).get("address")
            price = _num(p.get("priceUsd"))
            if mint not in chunk or not price:
                continue
            liq = _num((p.get("liquidity") or {}).get("usd")) or 0.0
            if mint not in best or liq > best[mint][0]:
                best[mint] = (liq, p, price)
        for mint, (liq, p, price) in best.items():
            created = p.get("pairCreatedAt")
            h1 = (p.get("txns") or {}).get("h1") or {}
            out[mint] = {
                "price": price,
                "liquidity": liq,
                "symbol": (p.get("baseToken") or {}).get("symbol"),
                "market_cap": _num(p.get("marketCap")) or _num(p.get("fdv")),
                "age_hours": round((time.time() * 1000 - created) / 3.6e6, 1) if created else None,
                "change_1h_pct": _num((p.get("priceChange") or {}).get("h1")),
                "volume_1h_usd": _num((p.get("volume") or {}).get("h1")),
                "buys_1h": h1.get("buys"),
                "sells_1h": h1.get("sells"),
                "txns_24h": sum(_num(((p.get("txns") or {}).get("h24") or {}).get(k)) or 0
                                for k in ("buys", "sells")),
            }
    return out
