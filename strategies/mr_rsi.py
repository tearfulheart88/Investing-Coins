"""
Strategy: RSI oversold mean reversion (v6)
Scenario ID: mr_rsi
"""

import logging
import time
from datetime import datetime, timedelta, timezone

from data.market_data import MarketData
from exchange.upbit_client import DataFetchError
from strategies.base_strategy import BaseStrategy, BuySignal, SellSignal

logger = logging.getLogger(__name__)

_INTERVAL = "minute60"
_HTF_INTERVAL = "minute240"
_HTF_EMA_PERIOD = 200
_RSI_PERIOD = 14
_RSI_BUY = 30.0
_RSI_BUY_RANGE = 32.0
_RSI_SELL = 65.0
_ADX_RANGE_THR = 15.0
_MAX_HOLD_HOURS = 24.0
_COOLDOWN_HOURS = 4.0
_TRAIL_TRIGGER_PCT = 2.5     # v6: 1.5->2.5
_TRAIL_DROP_PCT = 1.5        # v6: 1.0->1.5
_HARD_SL_PCT = 7.0
_BREAKEVEN_TRIGGER_PCT = 1.0  # v6: peak PnL% to activate floor
_BREAKEVEN_LOCK_PCT = 0.2     # v6: lock entry + 0.2%
_MAX_HOLD_EXTEND_HOURS = 12.0 # v6: grace period for MAX_HOLD

_KST = timezone(timedelta(hours=9))


class RSIStrategy(BaseStrategy):
    def __init__(self, market_data: MarketData) -> None:
        self._md = market_data
        self._cooldowns: dict[str, float] = {}
        self._peaks: dict[str, float] = {}

    def get_strategy_id(self) -> str:
        return "mean_reversion"

    def get_scenario_id(self) -> str:
        return "mr_rsi"

    def requires_scheduled_sell(self) -> bool:
        return False

    def get_history_requirements(self) -> dict[str, int]:
        return {
            _INTERVAL: 60,
            _HTF_INTERVAL: 195,
        }

    def get_ticker_selection_profile(self) -> dict:
        return {
            "pattern": "mean_reversion_rsi",
            "pool_size": 70,
            "refresh_hours": 1.0,
        }

    def on_position_closed(self, ticker: str, reason: str = "") -> None:
        self._peaks.pop(ticker, None)
        self._cooldowns[ticker] = time.time() + _COOLDOWN_HOURS * 3600
        logger.info(
            f"[mr_rsi] cooldown set | {ticker} | "
            f"{_COOLDOWN_HOURS}h lock | reason={reason}"
        )

    def on_position_reentered(
        self,
        ticker: str,
        new_entry_price: float,
        reason: str = "",
    ) -> None:
        # Reset the tracked peak to the new synthetic entry so trailing logic
        # does not reuse an old peak from the prior position lifecycle.
        self._peaks[ticker] = new_entry_price
        logger.info(
            f"[mr_rsi] re-entry context reset | {ticker} | "
            f"entry={new_entry_price:,.0f} | reason={reason}"
        )

    def _build_signal_trace(
        self,
        ticker: str,
        current_price: float,
        *,
        should_buy: bool,
        reason: str,
        values: dict | None = None,
        include_market_context: bool = True,
        include_rsi_series: bool = False,
    ) -> dict:
        trace = {
            "trace_version": "1.0",
            "trace_type": "buy_evaluation",
            "strategy_id": self.get_strategy_id(),
            "scenario_id": self.get_scenario_id(),
            "ticker": ticker,
            "evaluated_at": datetime.now(_KST).isoformat(),
            "current_price": float(current_price),
            "should_buy": bool(should_buy),
            "reason": reason,
            "values": values or {},
        }
        if include_rsi_series:
            try:
                rsi_series = self._md.compute_rsi_series_intraday(
                    ticker, _RSI_PERIOD, _INTERVAL, n=3
                )
                trace["rsi_series_1h"] = [round(value, 4) for value in rsi_series]
            except Exception as exc:
                trace["rsi_series_error"] = str(exc)
        if include_market_context:
            try:
                trace["market_data_context"] = self._md.build_signal_debug_context(
                    ticker,
                    [_INTERVAL, _HTF_INTERVAL],
                )
            except Exception as exc:
                trace["market_data_context_error"] = str(exc)
        return trace

    def _build_sell_signal_trace(
        self,
        ticker: str,
        current_price: float,
        position,
        *,
        should_sell: bool,
        reason: str,
        values: dict | None = None,
        include_market_context: bool = False,
    ) -> dict:
        entry = float(getattr(position, "buy_price", 0.0) or 0.0)
        peak = float(self._peaks.get(ticker, current_price) or current_price)
        trace = {
            "trace_version": "1.0",
            "trace_type": "sell_evaluation",
            "strategy_id": self.get_strategy_id(),
            "scenario_id": self.get_scenario_id(),
            "ticker": ticker,
            "evaluated_at": datetime.now(_KST).isoformat(),
            "current_price": float(current_price),
            "entry_price": entry,
            "tracked_peak_price": peak,
            "should_sell": bool(should_sell),
            "reason": reason,
            "values": values or {},
            "buy_time": getattr(position, "buy_time", None),
        }
        if include_market_context:
            try:
                trace["market_data_context"] = self._md.build_signal_debug_context(
                    ticker,
                    [_INTERVAL],
                )
            except Exception as exc:
                trace["market_data_context_error"] = str(exc)
        return trace

    def should_buy(self, ticker: str, current_price: float) -> BuySignal:
        cd_end = self._cooldowns.get(ticker, 0.0)
        if cd_end > 0:
            now = time.time()
            if now < cd_end:
                remaining_min = (cd_end - now) / 60
                meta = {
                    "signal_trace": self._build_signal_trace(
                        ticker,
                        current_price,
                        should_buy=False,
                        reason=f"COOLDOWN({remaining_min:.0f}min)",
                        values={"cooldown_remaining_min": round(remaining_min, 2)},
                        include_market_context=False,
                    )
                }
                return BuySignal(
                    ticker=ticker,
                    should_buy=False,
                    current_price=current_price,
                    reason=f"COOLDOWN({remaining_min:.0f}min)",
                    metadata=meta,
                )
            del self._cooldowns[ticker]

        try:
            rsi = self._md.compute_rsi_intraday(ticker, _RSI_PERIOD, _INTERVAL)
            adx = self._md.compute_adx(ticker, 14, _INTERVAL)
            ema200_4h = self._md.compute_ema_intraday(
                ticker, _HTF_EMA_PERIOD, _HTF_INTERVAL
            )
        except DataFetchError as exc:
            logger.warning(f"[mr_rsi] data fetch failed: {ticker}: {exc}")
            meta = {
                "signal_trace": self._build_signal_trace(
                    ticker,
                    current_price,
                    should_buy=False,
                    reason="DATA_ERROR",
                    values={"error": str(exc)},
                )
            }
            return BuySignal(
                ticker=ticker,
                should_buy=False,
                current_price=current_price,
                reason="DATA_ERROR",
                metadata=meta,
            )

        meta = {
            "rsi_1h": round(rsi, 2),
            "adx": round(adx, 1),
            "ema200_4h": round(ema200_4h, 0),
            "tp_label": "RSI recovery",
        }

        if current_price < ema200_4h:
            reason = f"BELOW_EMA200_4H({current_price:.0f}<{ema200_4h:.0f})"
            meta["signal_trace"] = self._build_signal_trace(
                ticker,
                current_price,
                should_buy=False,
                reason=reason,
                values={
                    "rsi_1h": round(rsi, 4),
                    "adx_1h": round(adx, 4),
                    "ema200_4h": round(ema200_4h, 4),
                },
            )
            logger.debug(f"[mr_rsi] {ticker} filtered by 4h EMA200 | {reason}")
            return BuySignal(ticker, False, current_price, reason, metadata=meta)

        is_weak_range = adx < _ADX_RANGE_THR
        rsi_buy = _RSI_BUY_RANGE if is_weak_range else _RSI_BUY
        meta["rsi_buy_thr"] = rsi_buy

        should_buy = rsi <= rsi_buy
        reason = "RSI_OVERSOLD" if should_buy else f"RSI_NORMAL({rsi:.1f})"
        meta["signal_trace"] = self._build_signal_trace(
            ticker,
            current_price,
            should_buy=should_buy,
            reason=reason,
            values={
                "rsi_1h": round(rsi, 4),
                "rsi_buy_threshold": float(rsi_buy),
                "is_weak_range": is_weak_range,
                "adx_1h": round(adx, 4),
                "ema200_4h": round(ema200_4h, 4),
            },
            include_rsi_series=True,
        )

        log_fn = logger.info if should_buy else logger.debug
        log_fn(
            f"[mr_rsi] {ticker} | RSI(1h)={rsi:.1f} "
            f"threshold={rsi_buy} ({'range' if is_weak_range else 'base'}) "
            f"ADX={adx:.1f} EMA200_4h={ema200_4h:.0f} -> {reason}"
        )

        return BuySignal(
            ticker=ticker,
            should_buy=should_buy,
            current_price=current_price,
            reason=reason,
            metadata=meta,
        )

    def should_sell_on_signal(self, ticker, current_price, position) -> SellSignal:
        entry = position.buy_price
        pnl_pct = (current_price - entry) / entry * 100

        if pnl_pct <= -_HARD_SL_PCT:
            reason = f"HARD_SL(pnl={pnl_pct:+.2f}%<=-{_HARD_SL_PCT}%)"
            logger.info(
                f"[mr_rsi] hard stop | {ticker} | "
                f"entry={entry:,.0f} now={current_price:,.0f} | {reason}"
            )
            return SellSignal(
                ticker,
                True,
                current_price,
                reason,
                metadata={
                    "signal_trace": self._build_sell_signal_trace(
                        ticker,
                        current_price,
                        position,
                        should_sell=True,
                        reason=reason,
                        values={
                            "pnl_pct": round(pnl_pct, 4),
                            "hard_sl_pct": float(_HARD_SL_PCT),
                        },
                        include_market_context=True,
                    )
                },
            )

        # v6: locked_profit_price로 재시작 내구성 확보
        locked = float(getattr(position, "locked_profit_price", 0.0) or 0.0)
        peak = self._peaks.get(ticker, current_price)
        if locked > entry:
            peak = max(peak, locked)
        if current_price > peak:
            peak = current_price
        self._peaks[ticker] = peak
        peak_pnl_pct = (peak - entry) / entry * 100

        # --- 2. BREAKEVEN DEFENSE (v6) ---
        if peak_pnl_pct >= _BREAKEVEN_TRIGGER_PCT:
            floor_price = entry * (1 + _BREAKEVEN_LOCK_PCT / 100)
            # locked_profit_price를 position에 저장 → 재시작 후에도 유지
            current_locked = float(getattr(position, "locked_profit_price", 0.0) or 0.0)
            if current_locked < floor_price:
                try:
                    position.locked_profit_price = floor_price
                except Exception:
                    pass
            if current_price <= floor_price:
                reason = (
                    f"BREAKEVEN_FLOOR(peak={peak_pnl_pct:+.2f}%"
                    f"|floor={floor_price:,.0f}"
                    f"|pnl={pnl_pct:+.2f}%)"
                )
                logger.info(
                    f"[mr_rsi] breakeven defense | {ticker} | "
                    f"entry={entry:,.0f} peak={peak:,.0f} "
                    f"floor={floor_price:,.0f} now={current_price:,.0f} | {reason}"
                )
                return SellSignal(
                    ticker, True, current_price, reason,
                    metadata={"signal_trace": self._build_sell_signal_trace(
                        ticker, current_price, position,
                        should_sell=True, reason=reason,
                        values={
                            "pnl_pct": round(pnl_pct, 4),
                            "peak_pnl_pct": round(peak_pnl_pct, 4),
                            "floor_price": round(floor_price, 8),
                            "breakeven_trigger_pct": float(_BREAKEVEN_TRIGGER_PCT),
                            "breakeven_lock_pct": float(_BREAKEVEN_LOCK_PCT),
                        },
                        include_market_context=True,
                    )},
                )

        # --- 3. TRAILING STOP ---
        if peak_pnl_pct >= _TRAIL_TRIGGER_PCT:
            drop_from_peak = (peak - current_price) / peak * 100
            if drop_from_peak >= _TRAIL_DROP_PCT:
                reason = (
                    f"TRAIL_STOP(peak={peak_pnl_pct:+.2f}%"
                    f"|drop={drop_from_peak:.2f}%>={_TRAIL_DROP_PCT}%"
                    f"|pnl={pnl_pct:+.2f}%)"
                )
                logger.info(
                    f"[mr_rsi] trailing exit | {ticker} | "
                    f"entry={entry:,.0f} peak={peak:,.0f} now={current_price:,.0f} | {reason}"
                )
                return SellSignal(
                    ticker,
                    True,
                    current_price,
                    reason,
                    metadata={
                        "signal_trace": self._build_sell_signal_trace(
                            ticker,
                            current_price,
                            position,
                            should_sell=True,
                            reason=reason,
                            values={
                                "pnl_pct": round(pnl_pct, 4),
                                "peak_price": round(peak, 8),
                                "peak_pnl_pct": round(peak_pnl_pct, 4),
                                "drop_from_peak_pct": round(drop_from_peak, 4),
                                "trail_trigger_pct": float(_TRAIL_TRIGGER_PCT),
                                "trail_drop_pct": float(_TRAIL_DROP_PCT),
                            },
                            include_market_context=True,
                        )
                    },
                )

        try:
            rsi = self._md.compute_rsi_intraday(ticker, _RSI_PERIOD, _INTERVAL)
        except DataFetchError as exc:
            logger.warning(f"[mr_rsi] sell RSI fetch failed: {ticker}: {exc}")
            return SellSignal(
                ticker,
                False,
                current_price,
                "",
                metadata={
                    "signal_trace": self._build_sell_signal_trace(
                        ticker,
                        current_price,
                        position,
                        should_sell=False,
                        reason="SELL_DATA_ERROR",
                        values={"error": str(exc)},
                    )
                },
            )

        # v6: adaptive RSI threshold based on current PnL
        if pnl_pct >= 3.0:
            rsi_sell_thr = 60.0
        elif pnl_pct >= 1.0:
            rsi_sell_thr = 65.0
        else:
            rsi_sell_thr = 70.0

        if rsi >= rsi_sell_thr:
            reason = f"RSI_RECOVERED({rsi:.1f}>={rsi_sell_thr:.0f}|pnl={pnl_pct:+.2f}%)"
            logger.info(
                f"[mr_rsi] exit on RSI recovery | {ticker} | "
                f"RSI(1h)={rsi:.1f} >= {rsi_sell_thr} | pnl={pnl_pct:+.2f}%"
            )
            return SellSignal(
                ticker,
                True,
                current_price,
                reason,
                metadata={
                    "signal_trace": self._build_sell_signal_trace(
                        ticker,
                        current_price,
                        position,
                        should_sell=True,
                        reason=reason,
                        values={
                            "pnl_pct": round(pnl_pct, 4),
                            "rsi_1h": round(rsi, 4),
                            "rsi_sell_threshold": float(rsi_sell_thr),
                            "adaptive_tier": "3pct" if pnl_pct >= 3 else ("1pct" if pnl_pct >= 1 else "base"),
                        },
                        include_market_context=True,
                    )
                },
            )

        try:
            buy_time = position.buy_time
            if isinstance(buy_time, str):
                buy_time = datetime.fromisoformat(buy_time)
            if buy_time.tzinfo is None:
                buy_time = buy_time.replace(tzinfo=_KST)
            elapsed_hours = (datetime.now(_KST) - buy_time).total_seconds() / 3600
            if elapsed_hours >= _MAX_HOLD_HOURS:
                # v6: extend if position is in recovery zone
                in_recovery = -2.0 < pnl_pct < 3.0 and rsi < 55.0
                effective_max = _MAX_HOLD_HOURS + (_MAX_HOLD_EXTEND_HOURS if in_recovery else 0)

                if elapsed_hours >= effective_max:
                    reason = (
                        f"MAX_HOLD_EXPIRED({elapsed_hours:.0f}h>={effective_max:.0f}h"
                        f"|pnl={pnl_pct:+.2f}%)"
                    )
                    logger.info(f"[mr_rsi] exit on max hold | {ticker} | {reason}")
                    return SellSignal(
                        ticker,
                        True,
                        current_price,
                        reason,
                        metadata={
                            "signal_trace": self._build_sell_signal_trace(
                                ticker,
                                current_price,
                                position,
                                should_sell=True,
                                reason=reason,
                                values={
                                    "pnl_pct": round(pnl_pct, 4),
                                    "elapsed_hours": round(elapsed_hours, 4),
                                    "base_max_hold": float(_MAX_HOLD_HOURS),
                                    "extended": in_recovery,
                                    "effective_max_hours": float(effective_max),
                                    "rsi_1h": round(rsi, 4),
                                },
                                include_market_context=True,
                            )
                        },
                    )
                else:
                    logger.info(
                        f"[mr_rsi] max-hold extended | {ticker} | "
                        f"{elapsed_hours:.1f}h / {effective_max:.0f}h | "
                        f"pnl={pnl_pct:+.2f}% RSI={rsi:.1f}"
                    )
        except Exception as exc:
            logger.debug(f"[mr_rsi] max-hold calculation error: {exc}")

        return SellSignal(ticker, False, current_price, "")
