# DipSnap — cbBTC dip-buying bot

[![make.fun · btc-race-24h-20261117-0603](https://make.fun/badge/btc-race-24h-20261117-0603.svg)](https://make.fun/bounty/btc-race-24h-20261117-0603?utm_source=github&utm_medium=readme&utm_campaign=btc-race-24h-20261117-0603)
[![rank](https://make.fun/badge/btc-race-24h-20261117-0603/bapenven-ctrl/btc-race-bot/rank.svg)](https://make.fun/bounty/btc-race-24h-20261117-0603/leaderboard?utm_source=github&utm_medium=readme&utm_campaign=btc-race-24h-20261117-0603)
[![reward](https://make.fun/badge/btc-race-24h-20261117-0603/bapenven-ctrl/btc-race-bot/reward.svg)](https://make.fun/bounty/btc-race-24h-20261117-0603?utm_source=github&utm_medium=readme&utm_campaign=btc-race-24h-20261117-0603)

A shallow-dip mean-reversion trading bot for the make.fun BTC race
(`btc-race-24h-20261117-0603`): it trades Coinbase Wrapped BTC (cbBTC)
against USDC on 1-minute on-chain bars with $1,000 of paper money, under
the `agent-fun/bars-replay/v1` protocol.

## Strategy

A full round trip costs roughly 26–38 bps in fees, slippage and price
impact, so the bot only trades snapbacks with room to clear that cost:

- **Entry** — the close is at least 1.0% below the rolling 90-bar high,
  RSI(14) has cooled to 46 or below, and a regime gate (the close may sit
  at most 1.2% under the 360-bar average) keeps it out of a collapse.
  It stakes 80% of equity, one position at a time.
- **Exits** — take-profit at +0.90% over the fee-inclusive cost, a
  trailing exit armed at +0.50% that fires 0.35% under the peak, a hard
  stop at −1.35%, and a 7-hour time stop, whichever comes first.
- **Discipline** — a cooldown after every exit (longer after a stop),
  no fresh entries in the final 40 bars, and a smaller late fallback
  entry so a quiet window still produces at least one trade.

Everything is a pure function of the bar stream — no clock, no network,
no randomness — so two executions on the same data produce identical
orders.

## Layout

- `main.py` — the bot (Python 3.13, standard library only).
- `vendor/makefun_bars.py` — the kit's protocol shim, byte-identical to
  `sdk/makefun_bars.py` in the competition kit.

Run it the way the grader does:

```
python3 -P -s main.py   # speaks newline-delimited JSON on stdin/stdout
```

## Licence

MIT — see `LICENSE`.
