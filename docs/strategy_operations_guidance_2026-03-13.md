# Strategy Operations Guidance - 2026-03-13

## Goal Alignment
- Primary goal: stable compounded growth with controlled drawdown.
- Secondary goal: preserve optionality for outsized wins without letting high-volatility strategies dominate real capital.
- Implication: real trading should remain quality-first, while fast breakout/pump strategies continue to mature in paper trading.

## Current Real-Trading Defaults
- `mr_rsi`: 50% budget, 10 tickers, 50% per trade
- `smrh_stop`: 50% budget, 10 tickers, 50% per trade

## What Is Good Right Now
- Real and paper ticker logs are separated.
- Analysis logs are separated into `real` and `paper`.
- Strategy-specific ticker selection profiles now exist.
- Strategy-specific universe refresh cadence now exists.
- `smrh_stop` now has better protection logic than before:
  - overheat filter
  - stop-gap filter
  - breakeven defense
  - trailing protection
  - MFE/MAE tracking

## What Still Needs Caution
- `smrh_stop` is still a breakout-oriented strategy, not a pure low-volatility core strategy.
- A `50/50` real allocation between `mr_rsi` and `smrh_stop` is acceptable for observation, but it is still aggressive for a long-term `+0.2%/day` style objective.
- Fast strategies (`pump_catcher`, scalp family) are still better kept in paper until trade count and payoff stability are proven.
- Global `STOP_LOSS_PCT=3%` remains an important engine-level fallback. Strategy-level stops should be preferred where possible.

## Recommended Refresh Cadence
- `pump_catcher`: 10 minutes
- `scalping_triple_ema`: 15 minutes
- `scalping_bb_rsi`: 15 minutes
- `scalping_5ema_reversal`: 15 minutes
- `smrh_stop`: 30 minutes
- `vb_noise_filter`: 30 minutes
- `vb_standard`: 30 minutes
- `mr_rsi`: 60 minutes
- `mr_bollinger`: 60 minutes
- `macd_rsi_trend`: 60 minutes

## Why These Cadences Fit
- Mean-reversion strategies use slower indicators and benefit from a more stable universe.
- Defensive breakout strategies should adapt faster than mean-reversion, but not churn like scalp systems.
- Pump/scalp strategies depend on short-lived event flow, so stale universes are especially harmful.

## Deployment Guidance By Strategy
- `mr_rsi`
  - Best suited for real trading core.
  - Keep selection conservative and refresh slower.
- `smrh_stop`
  - Suitable as a smaller real-trading satellite once enough real or paper evidence is accumulated.
  - Needs continued review of realized win rate, MFE capture, and stop efficiency.
- `vb_noise_filter`
  - Candidate for future real-trading promotion after more paper evidence.
  - Better suited than pump-style systems for controlled upside capture.
- `vb_standard`
  - More permissive than `vb_noise_filter`; treat as paper-first until stronger evidence appears.
- `scalping_triple_ema`
  - High opportunity, higher churn. Paper-first.
- `scalping_bb_rsi`
  - Useful for quick mean-reversion in ranges, but currently too noisy for core deployment.
- `scalping_5ema_reversal`
  - Fast and sensitive; paper-first.
- `pump_catcher`
  - Should be treated as an event strategy, not a core strategy.
  - Needs the widest candidate pool and the fastest refresh.
- `macd_rsi_trend`
  - Slower trend follower; keep in paper until enough completed trades accumulate.
- `mr_bollinger`
  - Conservative but still under-observed; paper-first for now.

## Misconfiguration Risks To Watch
- Too much real capital in breakout/event strategies causes a mismatch with stable-compounding goals.
- Too few candidate symbols in event strategies reduces the chance of catching the right coin at the right moment.
- Too slow a refresh cadence on fast strategies makes signal quality look worse than it really is.
- Too many fast strategies in real trading before paper validation increases noise and fee drag.

## Best Next Steps
- Keep real trading conservative for now.
- Continue collecting paper trade samples for all fast strategies.
- Promote strategies to real trading only after enough completed trades exist to judge:
  - win rate
  - average pnl
  - worst loss
  - MFE/MAE behavior
  - stability across multiple days

## Suggested Promotion Order
1. `mr_rsi`
2. `smrh_stop` (reduced weight preferred until evidence improves)
3. `vb_noise_filter`
4. `macd_rsi_trend`
5. scalp/pump strategies only after strong paper evidence
