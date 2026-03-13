# 2026-03-13 Batch 05 - Real/Paper Common Signal Trace Logging

## Why
- Real `mr_rsi` entered `KRW-KAVA`, but paper `mr_rsi` did not.
- We needed a shared trace format to compare indicator values, cached OHLCV state, and candle timestamps across real and paper runs.
- Plain console logs were not enough to explain future mismatches quickly.

## What Changed
- Added dedicated append-only signal trace logs:
  - `logs/signal_traces/real/signal_trace_YYYY-MM-DD.jsonl`
  - `logs/signal_traces/paper/signal_trace_YYYY-MM-DD.jsonl`
- Added market-data debug snapshots for intraday traces:
  - cache age
  - cache fetched time
  - last/previous candle timestamps
  - last/previous candle OHLCV
- `mr_rsi` now emits a common `signal_trace` payload on buy evaluation.
- Real and paper engines now persist those traces and keep the latest traces in session diagnostics.

## Notes
- This batch focuses on `mr_rsi`, because that is where the real/paper mismatch was observed.
- The structure is reusable for other strategies later without redesigning the trace format.
- Session analysis logs now include recent signal traces for easier post-run comparison.
