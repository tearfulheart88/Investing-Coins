# Update History - 2026-03-13 Batch 13

## Summary
- Added Telegram notifications for completed real SELLs and real-session stop summaries.
- Stored Telegram secrets in local `.env.local` so they stay out of GitHub uploads.
- Hardened `smrh_stop` using recent real-trade evidence:
  - stricter 30m volume requirement
  - tighter entry stop-gap filter
  - post-exit cooldowns
  - readable sub-1 KRW stop reasons
- Enriched real BUY/SELL metadata so stop summaries and notifications can include buy/sell price and realized PnL context.

## Why
- The user wanted stop-summary popups to also be delivered to Telegram.
- The user wanted every real completed sell to report:
  - which coin
  - buy price
  - sell price
  - realized KRW PnL
  - realized return
- Recent real logs showed:
  - `smrh_stop` cumulative bot-only performance was still negative
  - repeated churn losses on `KRW-STEEM`
  - poor readability for sub-1 KRW stops like `HARD_STOP_30M_LOW(0<0)`

## Recent Real-Trade Findings
- Bot-only completed sells by scenario at review time:
  - `smrh_stop`: 18 sells, 2 wins, total `-37,278.82 KRW`, average `-0.455%`
  - `mr_rsi`: 1 sell, total `-499.88 KRW`, average `-0.100%`
- Interpretation:
  - `mr_rsi` is still under-validated, not clearly broken.
  - `smrh_stop` remains the main realized-loss contributor and needed more conservative behavior.
  - Recent improvements were not all bad:
    - `KRW-NEAR` and `KRW-ETH` exited via `BREAKEVEN_DEFENSE` with realized profits.
  - But the cumulative profile still justified tightening `smrh_stop`.

## Code Changes
- `config.py`
  - Added `.env.local` loading after `.env` with override.
  - Added Telegram config fields:
    - `TELEGRAM_ENABLED`
    - `TELEGRAM_BOT_TOKEN`
    - `TELEGRAM_CHAT_ID`
    - `TELEGRAM_NOTIFY_REAL_SELLS`
    - `TELEGRAM_NOTIFY_REAL_STOP_SUMMARY`
  - Added `_env_bool()` helper.
- `.env.example`
  - Added Telegram placeholder keys.
- `.env.local`
  - Added local Telegram defaults.
  - This file is already ignored by `.gitignore`.
- `core/telegram_notifier.py`
  - Added Telegram send helper.
  - Added recent-update-based chat id auto-detection.
  - Added async helpers for:
    - real completed sells
    - real stop summaries
- `ui.py`
  - Added Telegram section in the `알림/기록` tab.
  - Added token/chat-id fields and on/off checkboxes.
  - Added "최근 대화 찾기" button for auto-detecting chat id from bot updates.
  - Added local `.env.local` persistence for Telegram settings.
  - Added stop-summary Telegram send on real stop popup.
- `core/trader.py`
  - BUY metadata now persists:
    - `buy_price`
    - `buy_time`
    - `buy_krw_spent`
    - applied stop fields
  - SELL metadata now persists:
    - buy context
    - sell time/avg price
    - realized KRW/PnL %
- `logging_/trade_logger.py`
  - Real completed sells now trigger Telegram notifications.
  - Historical/backfilled sell rows are excluded from Telegram spam.
- `strategies/smrh_stop.py`
  - `30m` volume filter tightened from `1.5x` to `1.8x`.
  - max structural stop-gap tightened from `1.5%` to `1.2%`.
  - cooldown added after exits:
    - loss/protective exits: `60m`
    - profit-protective exits: `30m`
  - sub-1 KRW prices now format correctly in stop reasons.

## Git Safety
- Telegram token is stored in `.env.local`.
- `.env.local` is ignored by `.gitignore`.
- The GitHub upload action also resets `.env.local` out of staging.

## Validation
- `python -m py_compile config.py core/telegram_notifier.py logging_/trade_logger.py core/trader.py strategies/smrh_stop.py ui.py`

