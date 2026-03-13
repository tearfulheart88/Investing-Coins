# 2026-03-13 Batch 07 - scalping_bb_rsi churn guard

## Why
- Paper `ACC-06` was repeatedly buying and selling the same ticker within seconds.
- Most exits happened at the same price level as the entry, so round-trip fees turned the trade into a small loss.
- This produced a fee-only churn loop instead of a real edge.

## What changed
- Rewrote `strategies/scalping_bb_rsi.py` in clean ASCII for easier maintenance.
- Added `target_edge` validation at entry time.
  - Skip entries when the distance to the Bollinger middle band is below `0.20%`.
- Added fee-aware exit validation.
  - Do not sell on `BB middle reached` unless the gross move is at least round-trip fees plus a small buffer.
- Added per-ticker cooldown after exits.
  - 5-minute cooldown before the same ticker can re-enter.
- Added re-entry distance guard.
  - Block new entries when price is still within `0.20%` of the last strategy exit price.
- Added clearer logs for:
  - cooldown blocks
  - thin-target skips
  - thin-edge sell deferrals

## Expected effect
- Stop same-price fee churn on low-volatility or low-priced names.
- Reduce immediate re-entry loops on the same ticker.
- Prefer fewer but cleaner scalping trades.

## Validation
- `python -m py_compile strategies/scalping_bb_rsi.py`

## Rollback
- Revert `strategies/scalping_bb_rsi.py` to the previous version if trade frequency becomes too low.
