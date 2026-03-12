# Strategy Allocation Plan

## Active Real Trading Default

- Live allocation is fixed to two stable strategies by default.
- `mr_rsi`: portfolio weight `50%`, per-trade budget `50%`, ticker count `10`
- `smrh_stop`: portfolio weight `50%`, per-trade budget `50%`, ticker count `10`

This default is meant to favor signal quality and capital preservation over trade frequency.

## Paper Trading Default Profiles

Paper accounts stay enabled for broad strategy evaluation and log accumulation.

- Stable
  - `mr_rsi`: ticker count `10`, per-trade budget `50%`
  - `mr_bollinger`: ticker count `10`, per-trade budget `50%`
  - `smrh_stop`: ticker count `10`, per-trade budget `50%`
- Neutral
  - `vb_noise_filter`: ticker count `10`, per-trade budget `50%`
  - `vb_standard`: ticker count `10`, per-trade budget `50%`
  - `macd_rsi_trend`: ticker count `10`, per-trade budget `50%`
- Aggressive evaluation only
  - `scalping_triple_ema`: ticker count `5`, per-trade budget `40%`
  - `scalping_bb_rsi`: ticker count `5`, per-trade budget `40%`
  - `scalping_5ema_reversal`: ticker count `5`, per-trade budget `40%`
  - `pump_catcher`: ticker count `3`, per-trade budget `30%`

Aggressive strategies are kept in paper mode only for now, so their win rate, drawdown, and execution quality can be reviewed before any live rollout.

## Ticker Universe Policy

- Tickers are not hardcoded per strategy.
- Each strategy builds its own live watchlist from the top 24h trade-value universe.
- Candidate lists are filtered by each strategy's minimum history requirement.
- Watchlists refresh every hour.
- Open positions stay subscribed and monitored until exit, even if they fall out of the refreshed universe.

## Suggested Future Live Templates

These are planning templates only and are not applied automatically.

- Conservative live: `mr_rsi 50%` + `smrh_stop 50%`
- Balanced live: stable strategies `70%`, neutral strategies `30%`
- Growth live: stable strategies `60%`, neutral or aggressive strategies `40%`

Future live expansion should only happen after paper data is sufficient to compare win rate, average pnl, drawdown, and exit-quality by strategy.
