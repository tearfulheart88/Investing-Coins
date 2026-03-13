# 2026-03-13 Batch 02 - Strategy-Specific Ticker Selection

## Why
- A single fixed `ticker_count` strategy was too blunt for very different entry styles.
- Event-driven strategies like `pump_catcher` need a much wider candidate pool than stable mean-reversion strategies.
- Real and paper refresh flows were still sharing a candidate pool sized only by `ticker_count`, so the new strategy-aware ranking logic was not fully active.

## What Changed
- Added `MarketData.get_ticker_selection_profile()` so refresh code can query the exact same strategy selection profile used by ticker ranking.
- Updated real trading and paper trading ticker refresh to size the shared candidate pool from the maximum strategy-specific `pool_size`, not just `ticker_count * 5`.
- Added `get_ticker_selection_profile()` declarations to major strategies so each strategy now explicitly declares:
  - which pattern it wants
  - how wide its candidate pool should be
- Expanded `pump_catcher` candidate pool from `150` to `180`.
- Increased the default paper watchlist size for `pump_catcher` from `3` to `10`.

## Strategy Profiles
- `vb_noise_filter`: `vol_breakout_filtered`, pool `80`
- `vb_standard`: `vol_breakout_basic`, pool `80`
- `mr_rsi`: `mean_reversion_rsi`, pool `70`
- `mr_bollinger`: `mean_reversion_band`, pool `70`
- `scalping_triple_ema`: `scalp_trend`, pool `100`
- `scalping_bb_rsi`: `scalp_range`, pool `100`
- `scalping_5ema_reversal`: `scalp_reversal`, pool `100`
- `macd_rsi_trend`: `trend_macd`, pool `80`
- `smrh_stop`: `trend_breakout_defensive`, pool `90`
- `pump_catcher`: `pump_event`, pool `180`

## Notes
- This batch changes how each strategy composes its candidate universe.
- Refresh cadence itself is still governed by the existing global ticker refresh setting.
- A later batch can add per-strategy refresh intervals if needed.

## Rollback
- Revert this batch if the wider per-strategy pools create unwanted universe churn.
- Main touched files:
  - `data/market_data.py`
  - `core/trader.py`
  - `core/paper_engine.py`
  - `strategies/*.py`
  - `config.py`
