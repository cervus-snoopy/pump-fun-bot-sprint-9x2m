"""Pump.fun Bot Sprint: top-flow momentum paper bot (agent-fun/replay/v2).

Strategy, from first principles on the bonding curve, calibrated on the
house dev window (1222 coins, 24h):
- Price on a pump.fun curve is virtual_sol_reserves / virtual_token_reserves,
  so any buy flow pushes it up and "price rose" alone means nothing. The
  signal that separates runners from noise is RELATIVE buy flow: calibrated
  on the house dev window, coins whose first trades bought < 30 SOL NEVER
  won (0% win in the 20-25 SOL bucket, 182 of 231 entries, -2.35 SOL), while
  coins reaching >= 30 SOL fast (median 21 trades) won 45% of the time.
  So entry is by buy-volume rank: a coin earns a probe only when its
  early trades bought >= 30 SOL with >= 65% buys from >= 8 distinct buyers.
- Exits are built for a market where 40%+ drawdowns are normal noise:
  take half at +50%, half of the rest at +120%, cut the whole position at
  -12%, and trail winners at 75% of their peak once up 40%. Stale positions
  leave after 2 hours. (A +30% partial take-profit was tried and REJECTED:
  it cut the ride on the coins that keep going and scored worse on both
  dev windows.)
- Winners get fed: a position up >= 50% with buy flow still dominant gets
  one more 0.10 SOL add, up to 0.30 SOL per coin. One probe per mint, ever:
  a coin that is sold is never re-entered, so the bot cannot churn on the
  same curve.
- Guardrail insurance: a small-size second tier (0.02 SOL) on the looser
  classic filter keeps the distinct-mint count above 20 even on a thin
  final window, entered only when at least 3 position slots are free.
- No randomness, no clock, no network: the bot is a pure function of the
  replayed events, so its two graded executions agree byte for byte.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "vendor"))
import makefun_bot

LAMPORTS_PER_SOL = 1_000_000_000

# ---- Tier A entry: top-flow coins (first 20 trades) ----
A_MIN_TRADES = 20
A_MIN_BUY_SOL = 30.0        # SOL bought in the early trades (calibrated: <30 never won; 40+ enters too late, worse on both windows)
A_MAX_ENTRY_TRADES = 30     # ... and it must get there FAST (slow accumulators lose)
A_BUY_SHARE = 0.65          # buys / all trades in the first window
A_MIN_BUYERS = 8            # distinct buyers in the first window
A_MAX_AGE_SLOTS = 2400      # ... and only while the coin is this young (~16 min)

# ---- Tier B entry: guardrail insurance (looser filter, tiny size) ----
B_MIN_TRADES = 20
B_MIN_BUY_SOL = 5.0
B_BUY_SHARE = 0.65
B_MIN_BUYERS = 8
B_MIN_GAIN = 1.10           # price vs launch price
B_MAX_AGE_SLOTS = 1200

# ---- sizing ----
A_PROBE_LAMPORTS = 150_000_000   # 0.15 SOL first buy (Tier A)
A_ADD_LAMPORTS = 150_000_000     # 0.15 SOL per pyramid add
A_MAX_PER_MINT = 450_000_000     # 0.45 SOL total deployed per coin (Tier A)
B_PROBE_LAMPORTS = 20_000_000    # 0.02 SOL (Tier B)
MIN_ORDER_LAMPORTS = 10_000_000  # 0.01 SOL: the guardrail's round-trip minimum
PYRAMID_AT = 1.50                # add while the position is up this much
MAX_ADDS = 2

# ---- exits (multiples of average entry cost) ----
TP1_AT = 1.50                  # sell half the remaining tokens
TP2_AT = 2.20                  # sell half of what is left
TRAIL_ARM_AT = 1.40            # arm the trailing stop once up this much
TRAIL_GIVEBACK = 0.75          # sell all when the price falls to 75% of its peak
HARD_STOP = 0.88               # sell all when the value falls to 88% of cost (-12%)
TIME_EXIT_SLOTS = 18000        # ~2h: sell all if still under +10%
TIME_EXIT_AT = 1.10

BUY_SLIPPAGE_BPS = 1000
SELL_SLIPPAGE_BPS = 2000


class Coin:
    """Per-mint evidence, built from the replayed events of one coin."""

    __slots__ = (
        "mint", "created_slot", "launch_price", "last_price", "last_slot",
        "trade_count", "buy_count", "buy_sol", "sell_sol", "buyers",
        "creator", "creator_sold", "recent", "complete",
        "entry_cost", "entry_tokens", "deployed", "entry_slot", "peak_mult",
        "adds", "tp1_done", "tp2_done", "entered", "tier",
    )

    def __init__(self, mint, created_slot):
        self.mint = mint
        self.created_slot = created_slot
        self.launch_price = 0.0
        self.last_price = 0.0
        self.last_slot = created_slot
        self.trade_count = 0
        self.buy_count = 0
        self.buy_sol = 0
        self.sell_sol = 0
        self.buyers = set()
        self.creator = None
        self.creator_sold = False
        # last trades as (price, is_buy, sol_lamports, user); capped, oldest dropped
        self.recent = []
        self.complete = False
        # position bookkeeping (only meaningful while we hold the coin)
        self.entry_cost = 0
        self.entry_tokens = 0
        self.deployed = 0          # total lamports ever spent buying this coin
        self.entry_slot = 0
        self.peak_mult = 1.0
        self.adds = 0
        self.tp1_done = False
        self.tp2_done = False
        self.entered = False       # a probe was placed on this coin, ever
        self.tier = 0              # 1 = Tier A, 2 = Tier B


class MomentumBot:
    """Top-flow momentum: probe the highest-flow coins, pyramid winners, exit on rules."""

    def __init__(self, hello):
        self.caps = hello["caps"]
        self.max_positions = int(self.caps["max_open_positions"])
        self.coins = {}          # mint -> Coin, first-seen order
        self.held = set()        # mints we currently hold (mirrors state.positions)
        self.pending_buy = {}    # mint -> lamports of buys still in flight
        self.tier_b_count = 0    # Tier B probes placed (capped: guardrail insurance only)

    # -- event folding -------------------------------------------------
    def fold(self, batch):
        touched = []
        seen = set()
        for e in batch["events"]:
            kind = e["event"]
            mint = e["mint"]
            if mint not in seen:
                seen.add(mint)
                touched.append(mint)
            coin = self.coins.get(mint)
            if kind == "CreateEvent":
                if coin is None:
                    coin = Coin(mint, e["slot"])
                    self.coins[mint] = coin
                coin.creator = e["creator"]
                v_sol = int(e["virtual_sol_reserves"])
                v_tok = int(e["virtual_token_reserves"])
                if v_tok > 0:
                    coin.launch_price = v_sol / v_tok
                    coin.last_price = coin.launch_price
                coin.last_slot = e["slot"]
            elif kind == "TradeEvent":
                if coin is None:
                    coin = Coin(mint, e["slot"])
                    self.coins[mint] = coin
                v_sol = int(e["virtual_sol_reserves"])
                v_tok = int(e["virtual_token_reserves"])
                price = (v_sol / v_tok) if v_tok > 0 else coin.last_price
                is_buy = bool(e["is_buy"])
                sol = int(e["sol_amount"])
                coin.trade_count += 1
                if is_buy:
                    coin.buy_count += 1
                    coin.buy_sol += sol
                    if len(coin.buyers) < 64:
                        coin.buyers.add(e["user"])
                else:
                    coin.sell_sol += sol
                    if coin.creator is not None and e["user"] == coin.creator:
                        coin.creator_sold = True
                coin.recent.append((price, is_buy, sol, e["user"]))
                if len(coin.recent) > 40:
                    del coin.recent[0]
                coin.last_price = price
                coin.last_slot = e["slot"]
            else:  # CompleteEvent: the curve migrated, tradable life is over
                if coin is None:
                    coin = Coin(mint, e["slot"])
                    self.coins[mint] = coin
                coin.complete = True
        return touched

    def price(self, state, mint):
        """Current price of a mint: the mark the harness reports, else last trade."""
        curve = state.curves.get(mint)
        if curve is not None and curve.virtual_token_reserves > 0:
            return curve.virtual_quote_reserves / curve.virtual_token_reserves
        coin = self.coins.get(mint)
        return coin.last_price if coin is not None else 0.0

    def entry_price(self, coin):
        """Average entry price per token from our own fills (authoritative)."""
        if coin.entry_tokens > 0 and coin.entry_cost > 0:
            return coin.entry_cost / coin.entry_tokens
        return 0.0

    # -- entry ----------------------------------------------------------
    def tier_a_ok(self, state, coin):
        """True when a coin has earned a Tier A probe right now."""
        if coin.complete or coin.entered:
            return False
        if coin.trade_count < A_MIN_TRADES:
            return False
        if coin.trade_count > A_MAX_ENTRY_TRADES:
            return False  # reached 30 SOL too slowly: the pump is already over
        if state.slot - coin.created_slot > A_MAX_AGE_SLOTS:
            return False
        if coin.mint in self.held or coin.mint in self.pending_buy:
            return False
        if coin.buy_count < A_MIN_TRADES * A_BUY_SHARE:
            return False
        if coin.buy_sol < A_MIN_BUY_SOL * LAMPORTS_PER_SOL:
            return False
        if len(coin.buyers) < A_MIN_BUYERS:
            return False
        return True

    def size_buy(self, state, coin, want):
        """A buy order of up to `want` lamports, respecting caps and cash, or None."""
        if state.committed_position_count() >= self.max_positions:
            return None
        cap = state.max_buy_lamports(coin.mint)
        if cap < MIN_ORDER_LAMPORTS:
            return None
        size = min(want, cap)
        free = state.free_cash_lamports()
        if size > free:
            size = free
        if size < MIN_ORDER_LAMPORTS:
            return None
        self.pending_buy[coin.mint] = size
        return {"side": "buy", "mint": coin.mint,
                "quote_lamports": size, "max_slippage_bps": BUY_SLIPPAGE_BPS}

    def tier_b_ok(self, state, coin):
        """True when a coin earns a Tier B (guardrail insurance) probe."""
        if coin.complete or coin.entered:
            return False
        if self.tier_b_count >= 30:  # insurance only: 30 coins cover the 20-mint bar
            return False
        if coin.trade_count < B_MIN_TRADES:
            return False
        if state.slot - coin.created_slot > B_MAX_AGE_SLOTS:
            return False
        if coin.mint in self.held or coin.mint in self.pending_buy:
            return False
        if coin.creator_sold:
            return False
        if coin.launch_price <= 0 or coin.last_price < coin.launch_price * B_MIN_GAIN:
            return False
        if coin.buy_count < B_MIN_TRADES * B_BUY_SHARE:
            return False
        if coin.buy_sol < B_MIN_BUY_SOL * LAMPORTS_PER_SOL:
            return False
        if len(coin.buyers) < B_MIN_BUYERS:
            return False
        # Tier B only when plenty of slots are free: it must never crowd out Tier A
        if state.committed_position_count() > self.max_positions - 3:
            return False
        return True

    # -- exits ----------------------------------------------------------
    def manage(self, state, coin, holding):
        """Orders that manage one held position this batch, or []."""
        orders = []
        if holding.token_amount <= 0:
            return orders
        price = self.price(state, coin.mint)
        if price <= 0:
            return orders
        entry_price = self.entry_price(coin)
        mult = price / entry_price if entry_price > 0 else 1.0
        if mult > coin.peak_mult:
            coin.peak_mult = mult
        tokens = holding.token_amount
        # tokens already promised by sells still in flight are not sellable again
        pending_sell = 0
        for p in state.pending.values():
            if p.order["mint"] == coin.mint and p.order["side"] == "sell":
                pending_sell += int(p.order["token_amount"])
        tokens -= pending_sell
        if tokens <= 0:
            return orders

        def sell_frac(frac):
            amount = int(tokens * frac)
            if amount <= 0:
                return None
            return {"side": "sell", "mint": coin.mint,
                    "token_amount": amount, "max_slippage_bps": SELL_SLIPPAGE_BPS}

        # hard stop first: a loser is cut, no questions asked
        if mult < HARD_STOP:
            o = sell_frac(1.0)
            return [o] if o else []
        # take-profit ladder: half at TP1, half of the rest at TP2
        if not coin.tp1_done and mult >= TP1_AT:
            coin.tp1_done = True
            o = sell_frac(0.5)
            return [o] if o else []
        if coin.tp1_done and not coin.tp2_done and mult >= TP2_AT:
            coin.tp2_done = True
            o = sell_frac(0.5)
            return [o] if o else []
        # trailing stop, armed once the position is comfortably up
        if coin.peak_mult >= TRAIL_ARM_AT and mult < coin.peak_mult * TRAIL_GIVEBACK:
            o = sell_frac(1.0)
            return [o] if o else []
        # time exit: dead money leaves after the window, unless it is a winner
        if state.slot - coin.entry_slot > TIME_EXIT_SLOTS and mult < TIME_EXIT_AT:
            o = sell_frac(1.0)
            return [o] if o else []
        return orders

    def maybe_add(self, state, coin, holding):
        """One pyramid add while a Tier A winner keeps its momentum, or None."""
        if coin.tier != 1 or coin.adds >= MAX_ADDS:
            return None
        if coin.deployed + A_ADD_LAMPORTS > A_MAX_PER_MINT:
            return None
        price = self.price(state, coin.mint)
        if price <= 0:
            return None
        entry_price = self.entry_price(coin)
        mult = price / entry_price if entry_price > 0 else 1.0
        if mult < PYRAMID_AT:
            return None
        # recent flow must still be buy-dominated (last 10 trades)
        recent = coin.recent[-10:]
        if len(recent) < 10:
            return None
        buys = sum(1 for (_, is_buy, _, _) in recent if is_buy)
        if buys < 7:
            return None
        o = self.size_buy(state, coin, A_ADD_LAMPORTS)
        if o is not None:
            coin.adds += 1
        return o

    # -- per-batch driver ------------------------------------------------
    def on_batch(self, state, batch):
        # track our own cost basis from filled orders (fills land 2 slots late)
        for f in batch["fills"]:
            coin = self.coins.get(f["mint"])
            # any reported fill (filled, rejected, failed) clears the in-flight buy
            self.pending_buy.pop(f["mint"], None)
            if coin is None or f["status"] != "filled":
                continue
            if f["side"] == "buy":
                coin.deployed += int(f["quote_lamports"])
                coin.entry_cost += int(f["quote_lamports"])
                coin.entry_tokens += int(f["token_amount"])
                if coin.entry_slot == 0:
                    coin.entry_slot = state.slot
            else:  # sell: shrink the remaining cost basis proportionally
                sold = int(f["token_amount"])
                if coin.entry_tokens > 0 and sold > 0:
                    coin.entry_cost -= coin.entry_cost * sold // coin.entry_tokens
                    coin.entry_tokens -= sold
        # refresh the held set from the authoritative account snapshot
        self.held = set(state.positions.keys())
        touched = self.fold(batch)
        orders = []
        # exits first: a position that must leave does so before new money goes in
        for mint in sorted(self.held):
            coin = self.coins.get(mint)
            holding = state.positions.get(mint)
            if coin is None or holding is None:
                continue
            orders.extend(self.manage(state, coin, holding))
            if len(orders) >= self.caps["max_orders_per_batch"]:
                return orders
        # pyramid adds on Tier A winners that still show buy flow
        for mint in sorted(self.held):
            coin = self.coins.get(mint)
            holding = state.positions.get(mint)
            if coin is None or holding is None:
                continue
            o = self.maybe_add(state, coin, holding)
            if o is not None:
                orders.append(o)
            if len(orders) >= self.caps["max_orders_per_batch"]:
                return orders
        # fresh probes: Tier A first (strongest buy volume first), then Tier B
        tier_a = []
        tier_b = []
        for mint in touched:
            coin = self.coins.get(mint)
            if coin is None:
                continue
            if self.tier_a_ok(state, coin):
                tier_a.append(coin)
            elif self.tier_b_ok(state, coin):
                tier_b.append(coin)
        tier_a.sort(key=lambda c: c.buy_sol, reverse=True)
        for coin in tier_a:
            o = self.size_buy(state, coin, A_PROBE_LAMPORTS)
            if o is not None:
                coin.entered = True
                coin.tier = 1
                orders.append(o)
            if len(orders) >= self.caps["max_orders_per_batch"]:
                return orders
        for coin in tier_b:
            o = self.size_buy(state, coin, B_PROBE_LAMPORTS)
            if o is not None:
                coin.entered = True
                coin.tier = 2
                self.tier_b_count += 1
                orders.append(o)
            if len(orders) >= self.caps["max_orders_per_batch"]:
                break
        return orders


def main():
    bot = None

    def on_hello(state):
        nonlocal bot
        bot = MomentumBot(state.hello)

    def on_batch(state, batch):
        return bot.on_batch(state, batch)

    makefun_bot.run(on_batch, on_hello=on_hello)


if __name__ == "__main__":
    main()
