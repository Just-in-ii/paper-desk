"""The pretend portfolio: cash, positions, limits, fills and exits.

No real money and no wallet. Every buy and sell is written to trades.csv.
"""
import csv
import datetime
import json
import os
import time

import config

TRADE_COLUMNS = ["time", "action", "symbol", "leader", "price", "qty",
                 "cash_flow_usd", "pnl_usd", "reason", "mint"]


def now():
    return time.time()


def stamp(t=None):
    t = now() if t is None else t
    return datetime.datetime.fromtimestamp(t, datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def today(t=None):
    t = now() if t is None else t
    return datetime.datetime.fromtimestamp(t, datetime.timezone.utc).strftime("%Y-%m-%d")


def append_csv(path, columns, row):
    new = not os.path.exists(path) or os.path.getsize(path) == 0
    with open(path, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=columns)
        if new:
            w.writeheader()
        w.writerow(row)


def fmt(x, places=10):
    return ("%.*g" % (places, x)) if isinstance(x, float) else x


class Paper:
    def __init__(self, path=None):
        self.path = path or config.STATE_FILE
        if os.path.exists(self.path):
            with open(self.path) as f:
                self.s = json.load(f)
        else:
            self.s = {"started": int(now()), "start_cash": config.START_POT_USD,
                      "cash": config.START_POT_USD, "positions": {}, "last_sig": {},
                      "left_at": {}, "day": today(), "buys_today": 0}
        for key, default in (("positions", {}), ("last_sig", {}), ("left_at", {}),
                             ("buys_today", 0), ("day", today())):
            self.s.setdefault(key, default)

    @property
    def positions(self):
        return self.s["positions"]

    @property
    def cash(self):
        return self.s["cash"]

    def save(self):
        tmp = self.path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(self.s, f, indent=1, sort_keys=True)
        os.replace(tmp, self.path)

    # ----- limits -----------------------------------------------------------

    def blocked(self, mint):
        """Why the desk itself won't buy this coin right now, or None."""
        if self.s["day"] != today():
            self.s["day"] = today()
            self.s["buys_today"] = 0
        if mint in self.positions:
            return "already_holding"
        left = self.s["left_at"].get(mint)
        if left and now() - left < config.REBUY_COOLDOWN_HOURS * 3600:
            return "cooldown"
        if len(self.positions) >= config.MAX_POSITIONS:
            return "max_positions"
        if self.s["buys_today"] >= config.MAX_BUYS_PER_DAY:
            return "daily_limit"
        if self.cash < config.TICKET_USD + config.FEE_USD:
            return "out_of_cash"
        return None

    # ----- trading ----------------------------------------------------------

    def _log(self, action, mint, pos, price, qty, cash_flow, pnl, reason):
        append_csv(config.TRADES_FILE, TRADE_COLUMNS, {
            "time": stamp(), "action": action, "symbol": pos["symbol"],
            "leader": pos["leader"], "price": fmt(price), "qty": fmt(qty),
            "cash_flow_usd": "%.4f" % cash_flow,
            "pnl_usd": "" if pnl is None else "%.4f" % pnl,
            "reason": reason, "mint": mint})

    def buy(self, mint, price, symbol, leader, reason):
        fill = price * (1 + config.SLIPPAGE_PCT / 100.0)
        qty = config.TICKET_USD / fill
        cost = config.TICKET_USD + config.FEE_USD
        self.s["cash"] -= cost
        self.s["buys_today"] += 1
        pos = {"symbol": symbol, "leader": leader, "opened": int(now()), "entry": fill,
               "qty": qty, "cost": cost, "tp_done": False, "misses": 0, "last_price": price}
        self.positions[mint] = pos
        self._log("BUY", mint, pos, fill, qty, -cost, None, reason)
        return pos

    def sell(self, mint, fraction, price, reason):
        """Sell a share of a position. price None = use the last known price."""
        pos = self.positions.get(mint)
        if not pos:
            return None
        if price is None:
            price = pos.get("last_price") or 0.0
        fraction = max(0.0, min(1.0, float(fraction)))
        # If what would be left is dust, sell it all so the P&L adds up.
        if fraction >= 0.999 or pos["qty"] * (1 - fraction) * price < 0.01:
            fraction = 1.0
        qty = pos["qty"] * fraction
        fill = price * (1 - config.SLIPPAGE_PCT / 100.0) if price > 0 else 0.0
        proceeds = max(0.0, qty * fill - config.FEE_USD) if fill > 0 else 0.0
        cost_share = pos["cost"] * fraction
        pnl = proceeds - cost_share
        self.s["cash"] += proceeds
        pos["qty"] -= qty
        pos["cost"] -= cost_share
        if price > 0:
            pos["last_price"] = price
        closed = fraction >= 1.0
        self._log("SELL" if closed else "PART SELL", mint, pos, fill, qty, proceeds, pnl, reason)
        if closed:
            del self.positions[mint]
            self.s["left_at"][mint] = int(now())
        return {"pnl": pnl, "proceeds": proceeds, "closed": closed}

    def follow_out(self, mint, fraction):
        """The leader sold but there's no market to sell into: sell at the first live price."""
        pos = self.positions.get(mint)
        if pos:
            pos["exit_pending"] = 1 - (1 - pos.get("exit_pending", 0.0)) * (1 - fraction)

    def manage_exits(self, quotes, quotes_ok):
        """Rug check, the leader's waiting exit, stop loss, take profit (once), then
        time limit. Returns console notes."""
        notes = []
        if not quotes_ok:
            if self.positions:
                notes.append("price check failed, open positions left as they are this run")
            return notes
        for mint in list(self.positions):
            pos = self.positions[mint]
            sym = pos["symbol"]
            q = quotes.get(mint)
            if q is None or (q.get("liquidity") or 0) < config.RUG_LIQUIDITY_USD:
                pos["misses"] = pos.get("misses", 0) + 1
                if pos["misses"] >= config.RUG_MISSES:
                    self.sell(mint, 1.0, 0.0, "rugged: market disappeared")
                    notes.append("%s written off at $0 (market gone %d runs)" % (sym, pos["misses"]))
                else:
                    notes.append("%s has no market (%d/%d)" % (sym, pos["misses"], config.RUG_MISSES))
                continue
            pos["misses"] = 0
            price = q["price"]
            pos["last_price"] = price
            if pos.get("exit_pending"):
                r = self.sell(mint, pos.pop("exit_pending"), price,
                              "%s sold earlier; first live price" % pos["leader"])
                notes.append("%s: followed %s out at the first live price" % (sym, pos["leader"]))
                if r["closed"]:
                    continue
            change = (price / pos["entry"] - 1) * 100
            if change <= config.STOP_LOSS_PCT:
                self.sell(mint, 1.0, price, "stop loss %+.0f%%" % change)
                notes.append("%s stop loss at %+.0f%%" % (sym, change))
                continue
            if not pos.get("tp_done") and change >= config.TAKE_PROFIT_PCT:
                pos["tp_done"] = True
                r = self.sell(mint, config.TAKE_PROFIT_SELL, price, "take profit %+.0f%%" % change)
                notes.append("%s take profit at %+.0f%%" % (sym, change))
                if r and r["closed"]:
                    continue
            if now() - pos["opened"] >= config.MAX_HOLD_HOURS * 3600:
                self.sell(mint, 1.0, price, "time limit %dh" % config.MAX_HOLD_HOURS)
                notes.append("%s closed on the time limit" % sym)
        return notes

    def value(self):
        """(cash, value of open positions at their last price)."""
        held = sum(p["qty"] * (p.get("last_price") or 0) for p in self.positions.values())
        return self.cash, held
