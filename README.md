[![Pump.fun Bot Sprint: Make Bot And Win](https://make.fun/badge/u-riidzhsn-pump-fun-bot-sprint-make-bot-and-win-cb203fed.svg)](https://make.fun/bounty/u-riidzhsn-pump-fun-bot-sprint-make-bot-and-win-cb203fed?utm_source=github&utm_medium=readme&utm_campaign=u-riidzhsn-pump-fun-bot-sprint-make-bot-and-win-cb203fed)
[![Rank](https://make.fun/badge/u-riidzhsn-pump-fun-bot-sprint-make-bot-and-win-cb203fed/cervus-snoopy/pump-fun-bot-sprint-9x2m/rank.svg)](https://make.fun/bounty/u-riidzhsn-pump-fun-bot-sprint-make-bot-and-win-cb203fed/leaderboard?utm_source=github&utm_medium=readme&utm_campaign=u-riidzhsn-pump-fun-bot-sprint-make-bot-and-win-cb203fed)
[![Reward](https://make.fun/badge/u-riidzhsn-pump-fun-bot-sprint-make-bot-and-win-cb203fed/cervus-snoopy/pump-fun-bot-sprint-9x2m/reward.svg)](https://make.fun/bounty/u-riidzhsn-pump-fun-bot-sprint-make-bot-and-win-cb203fed?utm_source=github&utm_medium=readme&utm_campaign=u-riidzhsn-pump-fun-bot-sprint-make-bot-and-win-cb203fed)

# Pump.fun Bot Sprint — Fast-Flow Momentum Bot

A paper-money trading bot for pump.fun bonding curves, built for the
[Make Bot And Win](https://make.fun/bounty/u-riidzhsn-pump-fun-bot-sprint-make-bot-and-win-cb203fed)
bounty on [make.fun](https://make.fun). It speaks `agent-fun/replay/v2`
(newline-delimited JSON on stdin/stdout) and trades 10 SOL of paper money on
a replay of recorded pump.fun chain data.

## Strategy

Price on a pump.fun curve is `virtual_sol_reserves / virtual_token_reserves`,
so any buy flow pushes it up — "price rose" alone means nothing. The edge is
selection, calibrated on the house dev windows:

- **Entry — fast, heavy buy flow only.** A coin earns a Tier A probe once its
  early trades bought ≥ 30 SOL, reaching that within its first 30 trades,
  with buys ≥ 65% of trades from ≥ 8 distinct buyers. Slow accumulators
  lose: coins that take longer to reach 30 SOL, or never reach it, won
  0–17% of the time in calibration; fast ones won 44%. Probe 0.15 SOL.
  At most 5 positions open at once.
- **Pyramid — add to winners only.** While a position is up ≥ 50% and its
  last 10 trades are still buy-dominated, add 0.15 SOL, up to 0.45 SOL total
  per coin.
- **Exits — ladder + trailing stop + hard stop.** Sell half at +50%, half of
  the rest at +120%; once up ≥ 40%, a fall to 75% of the peak sells all; a
  fall to 88% of entry cost sells all immediately; stale positions (2 h
  without reaching +10%) are closed.
- **Tier B — guardrail insurance.** A 0.02 SOL probe on the looser classic
  filter (≥ 5 SOL bought, ≥ +10% over launch, creator has not sold) keeps
  the distinct-mint count above the 20-coin bar even on a thin final window;
  capped at 30 coins and only when ≥ 3 position slots are free.

No randomness, no clock, no network: the bot is a pure function of the
replayed events, so its two graded executions agree byte for byte.

## Dev results (house kits, `grade-local --driver process`)

| version | dev window (v1 kit) | dev window (v2 kit, this competition) |
| --- | --- | --- |
| this bot | +0.24 SOL, 60 coins, 3.8% max drawdown | −0.27 SOL, 57 coins, 5.4% max drawdown |

Guardrails met on both windows (≥ 20 coins with a ≥ 0.01 SOL round trip,
drawdown well under 50%). The dev windows disagree — the edge is thin and
window-dependent, and the final window is a fresh 6 hours of tape the bot
has never seen. Stress test on the v2 window (20 rebuilt copies): median
−0.24 SOL, 16 of 20 copies lost money — this bot is a median loser, not a
champion. It is kept as a disciplined, guardrail-passing baseline, not a
claim to beat the field leaders (+1.07 / +0.82 / +0.70 SOL claimed dev).

## Run it

```sh
PYTHONHASHSEED=0 python3 -P -s main.py
```

`main.py` is the entrypoint (Python 3.13, standard library only).
`vendor/makefun_bot.py` is the kit's replay shim, vendored unchanged.

## Files

- `main.py` — the bot (strategy + wire-up)
- `vendor/makefun_bot.py` — the make.fun `agent-fun/replay/v2` shim
- `README.md` — this file
- `LICENSE` — MIT
