# Update History — 2026-03-13 Batch 12

## Summary
- Moved paper-trading startup initialization off the Tk UI thread.
- Added a `가상: 준비 중` startup state so the app no longer appears frozen while paper mode is building strategies, selecting tickers, and starting the WebSocket.

## Details
- `ui.py`
  - Added `_paper_starting` and `_paper_thread`.
  - Added `_collect_paper_start_payload()` so Tk variables are read on the UI thread first.
  - Replaced synchronous `_start_paper()` with a background-thread starter:
    - `_start_paper(payload)`
    - `_run_paper_engine(payload)`
    - `_on_paper_started()`
    - `_on_paper_start_failed(error)`
  - Updated stop/cleanup flow to reset startup state and clear `_paper_ws`.
  - Updated status indicator logic to display a separate startup state.

## Why
- The old paper start path did heavy work on the main Tk thread:
  - strategy loading
  - top-ticker pool fetch
  - per-strategy ticker selection
  - websocket boot
- That made the UI feel hung even when the startup was technically working.

## Verification
- `python -m py_compile ui.py`
