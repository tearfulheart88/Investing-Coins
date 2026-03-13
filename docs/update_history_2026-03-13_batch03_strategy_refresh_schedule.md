# 2026-03-13 Batch 03 - Strategy-Specific Refresh Schedule

## Why
- Strategy-specific ticker selection alone was not enough.
- Fast event-driven strategies need fresher universe rotation than 1-hour mean-reversion strategies.
- A single global refresh cadence was causing slow strategies and fast strategies to share the same timing assumptions.

## What Changed
- Added `refresh_hours` to strategy selection profiles.
- Real trading and paper trading now refresh scenario universes only when that specific strategy is due.
- Manual refresh still forces all active strategies to refresh immediately.
- Removed leftover global-only refresh bookkeeping from real/paper engines.

## Current Refresh Schedule
- `pump_catcher`: every `10` minutes
- `scalping_triple_ema`: every `15` minutes
- `scalping_bb_rsi`: every `15` minutes
- `scalping_5ema_reversal`: every `15` minutes
- `smrh_stop`: every `30` minutes
- `vb_noise_filter`: every `30` minutes
- `vb_standard`: every `30` minutes
- `mr_rsi`: every `60` minutes
- `mr_bollinger`: every `60` minutes
- `macd_rsi_trend`: every `60` minutes

## Notes
- This affects multi-scenario real trading and paper trading refresh flow.
- Single-strategy `DynamicTickerManager` flow still follows the existing global dynamic-ticker setting.
- Refresh timing is now strategy-aware, but portfolio weighting and live deployment defaults were not changed in this batch.

## Rollback
- Revert this batch if faster refresh cadence causes too much universe churn or API pressure.
- Main touched files:
  - `data/market_data.py`
  - `core/trader.py`
  - `core/paper_engine.py`
  - `strategies/*.py`
