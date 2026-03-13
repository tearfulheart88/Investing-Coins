# 2026-03-12 Batch 04 - `smrh_stop` Defensive Exit Upgrade

## Goal
- Reduce the chance that breakout trades round-trip from profit back into full losses.
- Prevent entries whose structural stop is too wide for a low-volatility compounding target.
- Record enough per-trade diagnostics to explain why a SELL happened.

## Changes
- Rebuilt `smrh_stop` with the original 4h trend / 30m trigger structure preserved.
- Added overheat filters:
  - skip when `rsi_4h > 80`
  - skip when `rsi_30m > 70`
- Added structural stop distance filter:
  - skip when the distance from entry price to `entry_30m_low` exceeds `1.5%`
- Added explicit engine backup stop metadata for the strategy:
  - bounded between `0.6%` and `1.2%`
  - no longer relies on the generic fallback by accident
- Added breakeven defense:
  - once peak profit reaches `+0.8%`, protect roughly `+0.2%`
- Added trailing protection:
  - arm at `+1.6%`
  - exit after `0.7%` pullback from peak
- Added runtime MFE/MAE tracking per position and attached it to completed real SELL logs
- Added SELL log metadata:
  - `entry_price`
  - `entry_stop_loss_price`
  - `locked_profit_price`
  - `hold_minutes`
  - strategy entry/runtime metadata including `mfe_pct` and `mae_pct`

## Files
- [smrh_stop.py](/C:/Users/user/Desktop/AI/GoogleDrive/Claude/Investing-Coins/strategies/smrh_stop.py)
- [trader.py](/C:/Users/user/Desktop/AI/GoogleDrive/Claude/Investing-Coins/core/trader.py)

## Rollback
- To roll back only the strategy behavior, restore the previous version of [smrh_stop.py](/C:/Users/user/Desktop/AI/GoogleDrive/Claude/Investing-Coins/strategies/smrh_stop.py).
- To roll back SELL diagnostics, remove the added `sell_metadata` assembly in [trader.py](/C:/Users/user/Desktop/AI/GoogleDrive/Claude/Investing-Coins/core/trader.py).
