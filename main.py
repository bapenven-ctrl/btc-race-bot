"""DipSnap — a shallow-dip mean-reversion bot for cbBTC (agent-fun/bars-replay/v1).

Strategy, in one paragraph: on a 1-minute bar stream where a full round trip
costs roughly 26-38 bps in fees, slippage and price impact, the only edge
worth taking is a snapback after a shallow dip. The bot stays in cash until
the latest close sits at least DIP_TRIGGER below the rolling 90-bar high
with RSI(14) cooled off, and a slow trend gate (close versus the 360-bar
average) says the market is dipping, not collapsing. It then buys a large
but not full slice of equity, and leaves on the first of: a take-profit
above its fee-inclusive cost, a trailing exit once the bounce has run, a
hard stop below cost, or a long time stop. A stop-out starts a cooldown so the same falling dip is
never bought twice, and a late, smaller fallback entry guarantees the bot
has traded at least once on a window where the primary dip never quite
fires. Everything is a pure function of the hello message and the batch
stream — no clock, no network, no randomness — so two runs on the same
bars produce byte-identical orders.

The wire protocol (hello, one batch per bar, one reply of orders per
batch) is spoken by the vendored kit shim in vendor/makefun_bars.py.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "vendor"))
import makefun_bars

SYMBOL = "CBBTC"

# --- Entry: what counts as a buyable dip -----------------------------------
HIGH_BARS = 90            # rolling high the dip is measured against
DIP_TRIGGER = 0.010       # buy when the close is >= 1.0% under that high
RSI_BARS = 14
RSI_ENTRY_MAX = 46.0      # RSI(14) must have cooled to this or below
TREND_BARS = 360          # slow average for the regime gate
TREND_FLOOR = 0.988       # close may sit at most 1.2% under the slow average
SIZE_BPS = 8000           # stake 80% of equity per entry
BUY_SLIPPAGE_BPS = 120    # refuse a fill more than 1.2% above the close

# --- Fallback entry: guarantees at least one trade on a quiet window -------
FALLBACK_FROM_SEQ = 720   # only in the second half of the window
FALLBACK_HIGH_BARS = 60
FALLBACK_DIP = 0.0030     # a shallower 0.30% dip is enough
FALLBACK_RSI_MAX = 55.0
FALLBACK_SIZE_BPS = 3500  # and only 35% of equity rides on it

# --- Exits ------------------------------------------------------------------
TAKE_PROFIT = 1.0090      # sell the whole position at +0.90% over cost
STOP_LOSS = 0.9865        # or at -1.35% under cost, no questions asked
TRAIL_ARM = 1.0050        # trailing exit arms once value reaches +0.50%
TRAIL_GIVEBACK = 0.9965   # and fires 0.35% under the peak value since entry
TIME_STOP_BARS = 420      # never hold one trade longer than 7 hours
SELL_SLIPPAGE_BPS = 200   # exits tolerate a 2% gap rather than stay stuck

# --- Housekeeping -----------------------------------------------------------
COOLDOWN_AFTER_STOP = 25  # bars to sit out after a stop-loss exit
COOLDOWN_AFTER_EXIT = 6   # bars to sit out after any other exit
LAST_ENTRY_SEQ = 1400     # no fresh entries in the final 40 bars


class DipSnap:
    """All strategy state, derived only from the hello and batch stream."""

    def __init__(self):
        self.closes = []            # every close seen, oldest first
        self.rsi = None             # Wilder RSI(14) state
        self._rsi_first = []
        self._rsi_gain = None
        self._rsi_loss = None
        self._last_close = None
        self.entry_seq = None       # batch seq at which the position appeared
        self.peak_ratio = 0.0       # best value/cost seen while holding
        self.cooldown_until = -1    # no entries while seq <= this
        self.trades_made = 0        # filled buys seen so far
        self.was_holding = False

    # -- indicators ----------------------------------------------------------
    def push_close(self, close):
        self.closes.append(close)
        if self._last_close is not None:
            change = close - self._last_close
            up = change if change > 0 else 0
            down = -change if change < 0 else 0
            if self._rsi_gain is None:
                self._rsi_first.append(change)
                if len(self._rsi_first) == RSI_BARS:
                    self._rsi_gain = sum(x for x in self._rsi_first if x > 0) / RSI_BARS
                    self._rsi_loss = sum(-x for x in self._rsi_first if x < 0) / RSI_BARS
            else:
                self._rsi_gain = (self._rsi_gain * (RSI_BARS - 1) + up) / RSI_BARS
                self._rsi_loss = (self._rsi_loss * (RSI_BARS - 1) + down) / RSI_BARS
            if self._rsi_gain is not None:
                moved = self._rsi_gain + self._rsi_loss
                self.rsi = 50.0 if moved == 0 else 100.0 * self._rsi_gain / moved
        self._last_close = close

    def average(self, bars):
        if len(self.closes) < bars:
            return None
        return sum(self.closes[-bars:]) / bars

    def high(self, bars):
        if len(self.closes) < bars:
            return None
        return max(self.closes[-bars:])

    # -- exits ----------------------------------------------------------------
    def exit_reason(self, state, held):
        """Why to sell the whole position on this bar, or None to keep holding."""
        value = state.value(SYMBOL, held.qty)
        if held.cost <= 0 or value <= 0:
            return None
        ratio = value / held.cost
        if ratio > self.peak_ratio:
            self.peak_ratio = ratio
        if ratio <= STOP_LOSS:
            return "stop"
        if ratio >= TAKE_PROFIT:
            return "target"
        if self.peak_ratio >= TRAIL_ARM and ratio <= self.peak_ratio * TRAIL_GIVEBACK:
            return "trail"
        if self.entry_seq is not None and state.seq - self.entry_seq >= TIME_STOP_BARS:
            return "time"
        return None

    # -- entries ----------------------------------------------------------------
    def entry_size(self, state, size_bps):
        """The buy notional for this bar: size_bps of equity, inside every cap."""
        stake = state.equity * size_bps // 10000
        room = state.weight_room(SYMBOL)
        if room < stake:
            stake = room
        if state.cash < stake:
            stake = state.cash
        minimum = int(state.hello["fees"]["min_notional_quote_units"])
        return stake if stake >= minimum else 0

    def dip_entry(self, state):
        """The primary mean-reversion entry, or 0 when this bar is no entry."""
        close = self.closes[-1]
        high = self.high(HIGH_BARS)
        slow = self.average(TREND_BARS)
        if high is None or slow is None or self.rsi is None:
            return 0
        if close > high * (1.0 - DIP_TRIGGER):
            return 0                    # not dipped far enough below the high
        if self.rsi > RSI_ENTRY_MAX:
            return 0                    # dip without a cooled RSI is a falling knife
        if close < slow * TREND_FLOOR:
            return 0                    # regime gate: a collapse, not a dip
        return self.entry_size(state, SIZE_BPS)

    def fallback_entry(self, state):
        """A smaller, shallower entry that only exists so a quiet window still trades."""
        if self.trades_made > 0 or state.seq < FALLBACK_FROM_SEQ:
            return 0
        close = self.closes[-1]
        high = self.high(FALLBACK_HIGH_BARS)
        if high is None or self.rsi is None:
            return 0
        if close > high * (1.0 - FALLBACK_DIP) or self.rsi > FALLBACK_RSI_MAX:
            return 0
        return self.entry_size(state, FALLBACK_SIZE_BPS)

    # -- the per-bar decision ----------------------------------------------------
    def on_batch(self, state, batch):
        for bar in batch["bars"]:
            if bar["symbol"] == SYMBOL:
                self.push_close(int(bar["c"]))
        if state.warming_up or not self.closes:
            return []

        held = state.positions.get(SYMBOL)
        if held is not None and not self.was_holding:
            self.entry_seq = state.seq     # the buy decided last bar has filled
            self.peak_ratio = 0.0
            self.trades_made += 1
        if held is None and self.was_holding:
            self.entry_seq = None          # the position is gone; exits are done
        self.was_holding = held is not None

        if held is not None:
            reason = self.exit_reason(state, held)
            if reason is None:
                return []
            bars = COOLDOWN_AFTER_STOP if reason == "stop" else COOLDOWN_AFTER_EXIT
            self.cooldown_until = state.seq + bars
            return [{"side": "sell", "symbol": SYMBOL, "qty": held.qty,
                     "max_slippage_bps": SELL_SLIPPAGE_BPS}]

        if state.seq <= self.cooldown_until or state.seq > LAST_ENTRY_SEQ:
            return []
        stake = self.dip_entry(state) or self.fallback_entry(state)
        if stake <= 0:
            return []
        return [{"side": "buy", "symbol": SYMBOL, "notional_quote_units": stake,
                 "max_slippage_bps": BUY_SLIPPAGE_BPS}]


BOT = DipSnap()

if __name__ == "__main__":
    makefun_bars.run(BOT.on_batch)
