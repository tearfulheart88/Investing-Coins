"""
Strategy: 15m Bollinger lower-band rebound scalper.
Scenario ID: scalping_bb_rsi

Intent:
- Trade ranging names only (low ADX).
- Enter after a lower-band sweep and a bullish recovery candle.
- Exit around the Bollinger middle band, but only if the move is large enough
  to clear round-trip fees with a small edge.

Churn protections added:
- Skip entries when the middle-band target is too close to the entry price.
- Enforce a short per-ticker cooldown after exits.
- Block immediate re-entry near the last exit price.
- Do not sell at the middle band if the gross edge is too small.
"""

import logging
import time

import config
from data.market_data import MarketData
from exchange.upbit_client import DataFetchError
from strategies.base_strategy import BaseStrategy, BuySignal, SellSignal

logger = logging.getLogger(__name__)

_INTERVAL = "minute15"
_BB_PERIOD = 20
_BB_STD = 2.0
_RSI_PERIOD = 14
_RSI_BUY = 38.0
_ADX_PERIOD = 14
_ADX_LIMIT = 25.0
_ATR_MULT = 1.2
_MAX_SL_PCT = 0.03
_FLEXIBLE_BB_ENTRY = True

# Need enough distance to the target to avoid fee-only round trips.
_MIN_TARGET_EDGE_PCT = 0.0020
# Require at least round-trip fees plus a small buffer before taking profit.
_MIN_EXIT_EDGE_PCT = max(config.FEE_RATE * 2 + 0.0005, 0.0015)
_REENTRY_COOLDOWN_SEC = 300
_REENTRY_MIN_MOVE_PCT = 0.0020


class ScalpingBBRSIStrategy(BaseStrategy):
    def __init__(self, market_data: MarketData) -> None:
        self._md = market_data
        self._cooldowns: dict[str, float] = {}
        self._last_exit_price: dict[str, float] = {}
        self._pending_exit_price: dict[str, float] = {}

    def get_strategy_id(self) -> str:
        return "scalping"

    def get_scenario_id(self) -> str:
        return "scalping_bb_rsi"

    def requires_scheduled_sell(self) -> bool:
        return False

    def get_history_requirements(self) -> dict[str, int]:
        return {
            _INTERVAL: 60,
        }

    def get_ticker_selection_profile(self) -> dict:
        return {
            "pattern": "scalp_range",
            "pool_size": 100,
            "refresh_hours": 0.25,
        }

    def on_position_closed(self, ticker: str, reason: str = "") -> None:
        self._cooldowns[ticker] = time.time() + _REENTRY_COOLDOWN_SEC
        exit_price = self._pending_exit_price.pop(ticker, None)
        if exit_price is not None:
            self._last_exit_price[ticker] = exit_price
        logger.info(
            f"[scalping_bb_rsi] cooldown registered | {ticker} | "
            f"{_REENTRY_COOLDOWN_SEC // 60:.0f}m | reason={reason}"
        )

    def should_buy(self, ticker: str, current_price: float) -> BuySignal:
        now = time.time()
        cooldown_until = self._cooldowns.get(ticker, 0.0)
        if cooldown_until > now:
            remaining_sec = int(cooldown_until - now)
            return BuySignal(
                ticker=ticker,
                should_buy=False,
                current_price=current_price,
                reason=f"COOLDOWN({remaining_sec}s)",
            )
        if cooldown_until:
            self._cooldowns.pop(ticker, None)

        try:
            adx = self._md.compute_adx(ticker, _ADX_PERIOD, _INTERVAL)
            upper, mid, lower = self._md.compute_bollinger_intraday(
                ticker, _BB_PERIOD, _BB_STD, _INTERVAL
            )
            rsi = self._md.compute_rsi_intraday(ticker, _RSI_PERIOD, _INTERVAL)
            raw_df = self._md.get_ohlcv_intraday(
                ticker, _INTERVAL, count=_BB_PERIOD + 10
            )
        except DataFetchError as e:
            logger.warning(f"[scalping_bb_rsi] data error: {ticker} - {e}")
            return BuySignal(ticker, False, current_price, "DATA_ERROR")

        meta = {
            "adx": round(adx, 1),
            "rsi_15m": round(rsi, 1),
            "bb_upper": round(upper, 0),
            "bb_middle": round(mid, 0),
            "bb_lower": round(lower, 0),
        }

        last_exit_price = self._last_exit_price.get(ticker)
        if last_exit_price:
            reentry_move_pct = abs(current_price - last_exit_price) / last_exit_price
            meta["last_exit_price"] = round(last_exit_price, 0)
            meta["reentry_move_pct"] = round(reentry_move_pct * 100, 3)
            if reentry_move_pct < _REENTRY_MIN_MOVE_PCT:
                return BuySignal(
                    ticker=ticker,
                    should_buy=False,
                    current_price=current_price,
                    reason=(
                        f"REENTRY_TOO_CLOSE({reentry_move_pct * 100:.2f}%"
                        f"<{_REENTRY_MIN_MOVE_PCT * 100:.2f}%)"
                    ),
                    metadata=meta,
                )

        if adx >= _ADX_LIMIT:
            logger.debug(
                f"[scalping_bb_rsi] {ticker} trending excluded ADX={adx:.1f} >= {_ADX_LIMIT}"
            )
            return BuySignal(
                ticker, False, current_price, f"TRENDING(ADX={adx:.1f})", metadata=meta
            )

        if len(raw_df) < 2:
            return BuySignal(ticker, False, current_price, "DATA_INSUFFICIENT", metadata=meta)

        prev_low = float(raw_df["low"].iloc[-2])
        bb_breached = prev_low < lower
        if not bb_breached:
            return BuySignal(ticker, False, current_price, "BB_NOT_BREACHED", metadata=meta)

        cur_open = float(raw_df["open"].iloc[-1])
        cur_close = float(raw_df["close"].iloc[-1])
        bullish = cur_close >= cur_open
        if not bullish:
            logger.debug(
                f"[scalping_bb_rsi] {ticker} waiting bullish candle "
                f"(BB lower={lower:.0f} RSI={rsi:.1f})"
            )
            return BuySignal(
                ticker, False, current_price, "WAITING_BULLISH_CANDLE", metadata=meta
            )

        if _FLEXIBLE_BB_ENTRY and bb_breached and bullish:
            if rsi >= _RSI_BUY:
                logger.info(
                    f"[scalping_bb_rsi] flexible entry | {ticker} | "
                    f"BB lower + bullish candle, RSI={rsi:.1f}>{_RSI_BUY}"
                )
        else:
            if rsi >= _RSI_BUY:
                return BuySignal(
                    ticker,
                    False,
                    current_price,
                    f"RSI_NOT_OVERSOLD({rsi:.1f})",
                    metadata=meta,
                )

        target_edge_pct = (mid - current_price) / current_price if current_price > 0 else 0.0
        meta["target_edge_pct"] = round(target_edge_pct * 100, 3)
        if target_edge_pct < _MIN_TARGET_EDGE_PCT:
            logger.info(
                f"[scalping_bb_rsi] skip thin target | {ticker} | "
                f"edge={target_edge_pct * 100:.2f}% < {_MIN_TARGET_EDGE_PCT * 100:.2f}% "
                f"(price={current_price:.0f}, mid={mid:.0f})"
            )
            return BuySignal(
                ticker=ticker,
                should_buy=False,
                current_price=current_price,
                reason=(
                    f"TP_EDGE_TOO_SMALL({target_edge_pct * 100:.2f}%"
                    f"<{_MIN_TARGET_EDGE_PCT * 100:.2f}%)"
                ),
                metadata=meta,
            )

        try:
            atr = self._md.compute_atr(ticker, period=14, interval=_INTERVAL)
            sl_pct = min(atr / current_price * _ATR_MULT, _MAX_SL_PCT)
        except DataFetchError:
            sl_pct = config.STOP_LOSS_PCT

        meta["stop_loss_pct"] = round(sl_pct, 6)
        meta["tp_price"] = round(mid, 0)
        meta["tp_label"] = "BB middle"

        logger.info(
            f"[scalping_bb_rsi] buy signal | {ticker} | "
            f"ADX={adx:.1f} RSI={rsi:.1f} BB lower={lower:.0f} "
            f"TP edge={target_edge_pct * 100:.2f}% SL={sl_pct:.2%}"
        )
        return BuySignal(
            ticker=ticker,
            should_buy=True,
            current_price=current_price,
            reason="BB_LOWER+RSI_OVERSOLD+BULLISH",
            metadata=meta,
        )

    def should_sell_on_signal(self, ticker, current_price, position) -> SellSignal:
        try:
            _, mid, _ = self._md.compute_bollinger_intraday(
                ticker, _BB_PERIOD, _BB_STD, _INTERVAL
            )
        except DataFetchError:
            return SellSignal(ticker, False, current_price, "")

        min_exit_price = position.buy_price * (1 + _MIN_EXIT_EDGE_PCT)
        if current_price >= mid and current_price >= min_exit_price:
            self._pending_exit_price[ticker] = current_price
            logger.info(
                f"[scalping_bb_rsi] sell on middle band | {ticker} | "
                f"price={current_price:.0f} mid={mid:.0f} "
                f"edge={(current_price / position.buy_price - 1) * 100:.2f}%"
            )
            return SellSignal(
                ticker,
                True,
                current_price,
                f"BB_MIDDLE_REACHED({mid:.0f}|min_exit={min_exit_price:.0f})",
            )

        if current_price >= mid:
            logger.info(
                f"[scalping_bb_rsi] middle band hit but edge too thin | {ticker} | "
                f"price={current_price:.0f} mid={mid:.0f} min_exit={min_exit_price:.0f}"
            )

        return SellSignal(ticker, False, current_price, "")
