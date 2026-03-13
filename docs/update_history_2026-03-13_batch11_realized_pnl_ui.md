# Update History — 2026-03-13 Batch 11

## Summary
- Added a realized-PnL panel to the `실제 포지션` tab so completed real SELL trades are visible in the UI.
- Added a real-session stop summary popup that shows the just-finished session's total PnL/return and realized PnL/return.
- Exposed a safe `Trader.get_session_summary()` helper for UI/session reporting.

## Details
- `ui.py`
  - Added `TradeDB` initialization in the desktop app.
  - Added recent completed real SELL table and summary text in the actual positions tab.
  - Added SQL-backed realized summary queries for bot-only completed SELL records.
  - Added stop-popup summary for the just-finished real-trading session.
  - Refreshes the realized-PnL panel on the normal UI polling loop.
- `core/trader.py`
  - Added `get_session_summary()` public helper.
  - Included `open_positions` in the session summary payload.

## Why
- The user wanted realized PnL from completed real trades to remain visible from the actual positions tab.
- The user also wanted an immediate summary of the just-finished real session when pressing stop.

## Verification
- `python -m py_compile ui.py core/trader.py`
