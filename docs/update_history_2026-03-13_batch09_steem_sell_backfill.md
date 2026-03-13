# 2026-03-13 Batch 09 - STEEM SELL Backfill

## Background
- `KRW-STEEM` churn trades on 2026-03-13 09:09~09:15 KST were actually sold on Upbit.
- Due to the `sell_metadata` `NameError`, those SELL records were missing from `logs/trades/trades.db` and downstream realized-performance reports.

## What was restored
- Matched 12 `KRW-STEEM` BUY rows in `trades.db` with 12 completed Upbit `ask` orders by executed volume.
- Backfilled 12 completed SELL records into the trade database/report pipeline.
- Kept the recovered SELL reason as `MACD_30M_NEG_PREV(-0.0169)` to reflect the original churn trigger.
- Tagged recovered rows in metadata with:
  - `backfilled=true`
  - `backfill_source=upbit_order_history`
  - `backfill_bug=sell_metadata_nameerror`
  - `matched_buy_uuid`

## Result
- Recovered STEEM completed sells: 12
- Recovered realized PnL: -16,138.58 KRW
- Average realized PnL per sell: -0.3605%
- Worst recovered sell: -0.6033%

## Notes
- This backfill is historical data repair only.
- The churn root cause itself is handled in `batch08` via:
  - `smrh_stop` immediate MACD-sell fix
  - same-signal-bar re-entry block
  - `smrh_stop` removal from default synthetic re-entry set
  - `sell_metadata` persistence fix in `core/trader.py`
