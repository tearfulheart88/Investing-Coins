# 2026-03-13 Batch 06 - Signal Trace Expansion

## Why
- The initial common trace logging only covered the normal `mr_rsi` buy evaluation path.
- We still needed to explain three common mismatch cases quickly:
  - strategy cooldown rejection
  - strategy data fetch failure
  - engine-level history guard rejection before the strategy ran

## What Changed
- Reworked `mr_rsi` signal trace generation into a reusable helper.
- `mr_rsi` now emits common trace payloads for:
  - `COOLDOWN(...)`
  - `DATA_ERROR`
  - `BELOW_EMA200_4H(...)`
  - `RSI_NORMAL(...)`
  - `RSI_OVERSOLD`
- Added engine-level `history_guard` traces to both real and paper engines.
- Those guard traces are only emitted when a new insufficient-history result is computed, so the JSONL files do not get spammed every loop.

## Result
- Real and paper runs now leave comparable trace evidence even when no buy happens.
- The next mismatch investigation can distinguish:
  - strategy rejected the setup
  - market data fetch failed
  - engine blocked the ticker before strategy evaluation
