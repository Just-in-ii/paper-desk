"""Decides COPY or SKIP for a coin a leader just bought.

Code kills on hard facts first. Only coins that survive can reach the
optional AI judge. A failed lookup always means SKIP, never COPY.
"""
import json
import os

import config
import net

ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"

SYSTEM_PROMPT = """You are the TOKEN DESK on a paper-trading experiment. A Solana wallet we follow just
bought a memecoin. Decide whether we follow with a small flat ticket. You never size and never time exits.

The coin already passed hard safety checks in code (no open mint/freeze authority, no danger flags,
holder concentration and liquidity within limits). Do not re-check those. Judge what code can't:
- do the warning-level risks add up to something dangerous together
- does the market look like a real crowd (buys vs sells, volume vs liquidity, age) or a few wallets
- is it already up so much in the last hour that we'd likely be buying someone's exit

Reply with ONLY this JSON, nothing else:
{"verdict": "COPY" or "SKIP", "confidence": 0.0 to 1.0, "why": "one short sentence", "main_risk": "one short sentence"}

Never answer COPY if a field you need is null. Never mention price targets."""


def _n(x):
    """Tidy number for reason strings: 45.0 -> 45, 45.25 -> 45.2."""
    x = float(x)
    return str(int(x)) if x == int(x) else ("%.1f" % x)


def fact_kill(rc, dx):
    """First failing safety rule as a reason string, or None if the coin passes."""
    t = config.TOKEN
    if rc is None:
        return "no_rugcheck_report"
    if rc.get("rugged"):
        return "rugged"
    if rc.get("mint_authority") or rc.get("freeze_authority"):
        return "authority_open"
    if rc.get("danger"):
        return "danger: %s" % rc["danger"][0]
    if rc.get("risk_score") is None:
        return "no_risk_score"
    if rc["risk_score"] > t["max_risk_score"]:
        return "risk_score %s" % _n(rc["risk_score"])
    if rc.get("top10_pct") is None:
        return "no_holder_data"
    if rc["top10_pct"] > t["max_top10_pct"]:
        return "top10_hold %s%%" % _n(rc["top10_pct"])
    if rc.get("lp_locked_pct") is not None and rc["lp_locked_pct"] < t["min_lp_locked_pct"]:
        return "lp_unlocked %s%%" % _n(rc["lp_locked_pct"])
    if (rc.get("insiders") or 0) > t["max_insiders"]:
        return "insiders %d" % rc["insiders"]
    if dx is None:
        return "no_price"
    if (dx.get("liquidity") or 0) < t["min_liquidity_usd"]:
        return "thin_liquidity $%d" % int(dx.get("liquidity") or 0)
    return None


def ai_verdict(leader, rc, dx):
    """Ask Claude for COPY/SKIP. Returns the parsed JSON dict. Raises on any failure."""
    payload = {"leader": leader, "token_safety": rc, "market": dx}
    body = {
        "model": config.AI_MODEL,
        "max_tokens": 300,
        # Thinking off so the short reply budget all goes on the answer.
        "thinking": {"type": "disabled"},
        "system": SYSTEM_PROMPT,
        "messages": [{"role": "user", "content": json.dumps(payload)}],
    }
    headers = {"x-api-key": os.environ.get("ANTHROPIC_API_KEY", ""),
               "anthropic-version": "2023-06-01"}
    reply = net.post_json(ANTHROPIC_URL, body, headers=headers, label="Anthropic API")
    if reply.get("stop_reason") == "refusal":
        raise ValueError("the model declined to answer")
    text = "".join(b.get("text", "") for b in reply.get("content") or []
                   if b.get("type") == "text")
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("no JSON in reply")
    return json.loads(text[start:end + 1])


def decide(leader, rc, dx):
    """(verdict, reason) where verdict is COPY or SKIP."""
    killed = fact_kill(rc, dx)
    if killed:
        return "SKIP", killed
    if not config.AI_JUDGE:
        return "COPY", "passed safety filter"
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return "SKIP", "AI_JUDGE is on but ANTHROPIC_API_KEY isn't set"
    try:
        v = ai_verdict(leader, rc, dx)
        verdict = str(v.get("verdict", "")).upper()
        confidence = float(v.get("confidence") or 0)
        why = str(v.get("why") or "").strip()
    except Exception as e:
        return "SKIP", "ai_failed: %s" % (str(e)[:120] or type(e).__name__)
    if verdict == "COPY":
        if confidence < config.AI_MIN_CONFIDENCE:
            return "SKIP", "ai_low_confidence %s" % confidence
        return "COPY", "ai: %s" % why
    return "SKIP", "ai_skip: %s" % why
