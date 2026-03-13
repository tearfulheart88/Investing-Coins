"""
Strategy: SMRH breakout stop
Scenario ID: smrh_stop

Design goals
- Keep the original 4h trend + 30m trigger idea.
- Reject overheated breakouts.
- Reject entries whose structural stop is too far away.
- Add breakeven defense and trailing protection so winners are not forced
  to round-trip back into full losses.
- Persist enough entry metadata so SELL logs can explain what happened.
"""

import logging
import time

from data.market_data import MarketData
from exchange.upbit_client import DataFetchError
from strategies.base_strategy import BaseStrategy, BuySignal, SellSignal

logger = logging.getLogger(__name__)

_INTERVAL_4H = "minute240"
_INTERVAL_30M = "minute30"

_MACD_FAST = 12
_MACD_SLOW = 26
_MACD_SIGNAL = 70

_STOCH_K = 12
_STOCH_D = 3
_STOCH_SMOOTH = 3

_RSI_PERIOD = 14
_RSI_MIN = 50.0
_MA_PERIOD = 20
_HA_WICK_PCT = 0.005

_VOL_MULT_30M = 1.8
_VOL_SMA_PERIOD = 20

_OVERHEAT_RSI_4H_MAX = 80.0
_OVERHEAT_RSI_30M_MAX = 70.0
_MAX_ENTRY_STOP_GAP_PCT = 1.2
_ENGINE_STOP_MIN_PCT = 0.6
_ENGINE_STOP_MAX_PCT = 1.2
_BREAKEVEN_TRIGGER_PCT = 0.8
_BREAKEVEN_LOCK_PCT = 0.2
_TRAIL_TRIGGER_PCT = 1.6
_TRAIL_DROP_PCT = 0.7
_REENTRY_LOCK_REASON = "SAME_SIGNAL_BAR"
_LOSS_EXIT_COOLDOWN_MIN = 60.0
_PROFIT_EXIT_COOLDOWN_MIN = 30.0


class SMRHStopStrategy(BaseStrategy):
    """Multi-timeframe breakout strategy with defensive exits."""

    def __init__(self, market_data: MarketData) -> None:
        self._md = market_data
        self._entry_lows: dict[str, float] = {}
        self._last_entry_bar_keys: dict[str, str] = {}
        self._cooldowns: dict[str, float] = {}

    def get_strategy_id(self) -> str:
        return "trend_following"

    def get_scenario_id(self) -> str:
        return "smrh_stop"

    def requires_scheduled_sell(self) -> bool:
        return False

    def get_history_requirements(self) -> dict[str, int]:
        return {
            "day": _MA_PERIOD,
            _INTERVAL_4H: max(_MACD_SLOW + _MACD_SIGNAL + 3, 56),
            _INTERVAL_30M: max(_MACD_SLOW + _MACD_SIGNAL + 3, 50),
        }

    def get_ticker_selection_profile(self) -> dict:
        return {
            "pattern": "trend_breakout_defensive",
            "pool_size": 90,
            "refresh_hours": 0.5,
        }

    def on_position_closed(self, ticker: str, reason: str = "") -> None:
        self._entry_lows.pop(ticker, None)
        cooldown_min = 0.0
        if any(tag in reason for tag in ("ENGINE_STOP", "HARD_STOP_30M_LOW", "MACD_30M_")):
            cooldown_min = _LOSS_EXIT_COOLDOWN_MIN
        elif any(tag in reason for tag in ("BREAKEVEN_DEFENSE", "TRAIL_STOP", "HA_4H_TURNED_BEAR")):
            cooldown_min = _PROFIT_EXIT_COOLDOWN_MIN

        if cooldown_min > 0:
            self._cooldowns[ticker] = time.time() + cooldown_min * 60.0
            logger.info(
                f"[smrh] cooldown set | {ticker} | "
                f"{cooldown_min:.0f}m lock | reason={reason}"
            )

    @staticmethod
    def _fmt_price(price: float) -> str:
        if price >= 100:
            return f"{price:,.0f}"
        if price >= 1:
            return f"{price:,.3f}"
        return f"{price:,.6f}"

    def _update_runtime_stats(self, position, current_price: float) -> dict:
        meta = position.entry_metadata if isinstance(position.entry_metadata, dict) else {}
        if position.entry_metadata is not meta:
            position.entry_metadata = meta

        entry_price = float(position.buy_price or 0.0)
        if entry_price <= 0:
            return meta

        peak_price = max(float(meta.get("peak_price", entry_price) or entry_price), current_price)
        worst_price = min(float(meta.get("worst_price", entry_price) or entry_price), current_price)
        peak_pnl_pct = (peak_price - entry_price) / entry_price * 100
        mae_pct = (worst_price - entry_price) / entry_price * 100
        drop_from_peak_pct = (peak_price - current_price) / peak_price * 100 if peak_price > 0 else 0.0

        meta["peak_price"] = peak_price
        meta["worst_price"] = worst_price
        meta["mfe_pct"] = round(peak_pnl_pct, 3)
        meta["mae_pct"] = round(mae_pct, 3)
        meta["peak_pnl_pct"] = round(peak_pnl_pct, 3)
        meta["drop_from_peak_pct"] = round(drop_from_peak_pct, 3)
        return meta

    def should_buy(self, ticker: str, current_price: float) -> BuySignal:
        cooldown_until = self._cooldowns.get(ticker, 0.0)
        if cooldown_until > time.time():
            remaining_min = (cooldown_until - time.time()) / 60.0
            return BuySignal(
                ticker,
                False,
                current_price,
                f"COOLDOWN({remaining_min:.0f}min)",
            )
        if cooldown_until:
            self._cooldowns.pop(ticker, None)

        try:
            macd_4h = self._md.compute_macd(
                ticker, _MACD_FAST, _MACD_SLOW, _MACD_SIGNAL, _INTERVAL_4H
            )
            stoch_4h = self._md.compute_stochastic(
                ticker, _STOCH_K, _STOCH_D, _STOCH_SMOOTH, _INTERVAL_4H
            )
            rsi_4h = self._md.compute_rsi_intraday(ticker, _RSI_PERIOD, _INTERVAL_4H)
            ma_20d = self._md.compute_ma(ticker, _MA_PERIOD)
            ha_4h = self._md.compute_ha_intraday(ticker, _INTERVAL_4H, count=10)

            macd_30m = self._md.compute_macd(
                ticker, _MACD_FAST, _MACD_SLOW, _MACD_SIGNAL, _INTERVAL_30M
            )
            stoch_30m = self._md.compute_stochastic(
                ticker, _STOCH_K, _STOCH_D, _STOCH_SMOOTH, _INTERVAL_30M
            )
            rsi_30m = self._md.compute_rsi_intraday(ticker, _RSI_PERIOD, _INTERVAL_30M)
            ha_30m = self._md.compute_ha_intraday(ticker, _INTERVAL_30M, count=50)
            vol_cur_30m, vol_sma_30m = self._md.compute_volume_sma_intraday(
                ticker, _VOL_SMA_PERIOD, _INTERVAL_30M
            )
        except DataFetchError as e:
            logger.warning(f"[smrh] data error: {ticker} - {e}")
            return BuySignal(ticker, False, current_price, "DATA_ERROR")

        ha_4h_last = ha_4h.iloc[-1]
        ha_4h_bullish = bool(ha_4h_last["is_bullish"])
        ha_open_4h = float(ha_4h_last["open"])
        ha_low_4h = float(ha_4h_last["low"])
        ha_4h_no_lower_wick = (
            ha_4h_bullish
            and (abs(ha_open_4h - ha_low_4h) / ha_open_4h < _HA_WICK_PCT if ha_open_4h > 0 else False)
        )

        meta = {
            "macd_4h_hist": round(macd_4h["hist"], 6),
            "stoch_4h_k": round(stoch_4h["k"], 1),
            "stoch_4h_d": round(stoch_4h["d"], 1),
            "rsi_4h": round(rsi_4h, 1),
            "ma_20d": round(ma_20d, 0),
            "ha_4h": "STRONG_BULL" if ha_4h_no_lower_wick else ("BULL" if ha_4h_bullish else "BEAR"),
            "ha_4h_open": round(ha_open_4h, 0),
            "ha_4h_low": round(ha_low_4h, 0),
            "macd_30m_hist": round(macd_30m["hist"], 6),
            "stoch_30m_k": round(stoch_30m["k"], 1),
            "stoch_30m_d": round(stoch_30m["d"], 1),
            "rsi_30m": round(rsi_30m, 1),
            "vol_ratio_30m": round(vol_cur_30m / vol_sma_30m if vol_sma_30m > 0 else 0.0, 2),
        }

        cond_4h_macd = macd_4h["hist"] > 0
        cond_4h_stoch = stoch_4h["k"] > stoch_4h["d"]
        cond_4h_rsi = rsi_4h >= _RSI_MIN
        cond_4h_ma = current_price >= ma_20d
        cond_4h_ha = ha_4h_no_lower_wick

        if not all([cond_4h_macd, cond_4h_stoch, cond_4h_rsi, cond_4h_ma, cond_4h_ha]):
            failed = []
            if not cond_4h_macd:
                failed.append(f"MACD_4H_NEG({macd_4h['hist']:.4f})")
            if not cond_4h_stoch:
                failed.append(f"STOCH_4H_K<D({stoch_4h['k']:.1f}<{stoch_4h['d']:.1f})")
            if not cond_4h_rsi:
                failed.append(f"RSI_4H_LOW({rsi_4h:.1f}<{_RSI_MIN})")
            if not cond_4h_ma:
                failed.append(f"BELOW_MA20({current_price:,.0f}<{ma_20d:,.0f})")
            if not cond_4h_ha:
                if not ha_4h_bullish:
                    failed.append("HA_4H_BEAR")
                else:
                    wick_ratio = abs(ha_open_4h - ha_low_4h) / ha_open_4h * 100 if ha_open_4h > 0 else 0.0
                    failed.append(f"HA_4H_HAS_LOWER_WICK({wick_ratio:.3f}%>={_HA_WICK_PCT*100:.3f}%)")
            logger.debug(f"[smrh] {ticker} | 4H_FAIL:" + ",".join(failed))
            return BuySignal(ticker, False, current_price, "4H_FILTER:" + ",".join(failed), metadata=meta)

        ha_30m_last = ha_30m.iloc[-1]
        signal_bar = getattr(ha_30m.index[-1], "isoformat", lambda: str(ha_30m.index[-1]))()
        cond_30m_rsi = rsi_30m >= _RSI_MIN
        cond_30m_ha = bool(ha_30m_last["is_bullish"]) or bool(ha_30m_last["turned_bullish"])
        macd_cross = macd_30m["hist_prev"] < 0 and macd_30m["hist"] > 0
        stoch_cross = (
            stoch_30m["k_prev"] <= stoch_30m["d_prev"]
            and stoch_30m["k"] > stoch_30m["d"]
        )
        cond_30m_cross = macd_cross or stoch_cross

        if not (cond_30m_rsi and cond_30m_ha and cond_30m_cross):
            failed = []
            if not cond_30m_rsi:
                failed.append(f"RSI_30M_LOW({rsi_30m:.1f})")
            if not cond_30m_ha:
                failed.append("HA_30M_BEAR")
            if not cond_30m_cross:
                failed.append(
                    f"NO_CROSS(MACD:{macd_30m['hist_prev']:.4f}->{macd_30m['hist']:.4f}"
                    f"|Stoch_K:{stoch_30m['k_prev']:.1f}->{stoch_30m['k']:.1f})"
                )
            logger.debug(f"[smrh] {ticker} | 30M_FAIL:" + ",".join(failed))
            return BuySignal(ticker, False, current_price, "30M_FILTER:" + ",".join(failed), metadata=meta)

        if rsi_4h > _OVERHEAT_RSI_4H_MAX or rsi_30m > _OVERHEAT_RSI_30M_MAX:
            reason = f"OVERHEAT(RSI_4H={rsi_4h:.1f}|RSI_30M={rsi_30m:.1f})"
            logger.info(
                f"[smrh] {ticker} | OVERHEAT skip | "
                f"RSI_4H={rsi_4h:.1f} RSI_30M={rsi_30m:.1f}"
            )
            return BuySignal(ticker, False, current_price, reason, metadata=meta)

        vol_ratio_30m = vol_cur_30m / vol_sma_30m if vol_sma_30m > 0 else 0.0
        if vol_ratio_30m < _VOL_MULT_30M:
            logger.debug(
                f"[smrh] {ticker} | VOL_WEAK_30M: "
                f"{vol_ratio_30m:.2f}x < {_VOL_MULT_30M}x"
            )
            return BuySignal(
                ticker,
                False,
                current_price,
                f"VOL_WEAK_30M({vol_ratio_30m:.2f}x<{_VOL_MULT_30M}x)",
                metadata=meta,
            )

        turned_rows = ha_30m[ha_30m["turned_bullish"]]
        if turned_rows.empty:
            logger.debug(f"[smrh] {ticker} | NO_HA_TURN_BULLISH")
            return BuySignal(ticker, False, current_price, "NO_HA_TURN_BULLISH", metadata=meta)

        breakout_target = float(turned_rows.iloc[-1]["high"])
        meta["ha_breakout_target"] = round(breakout_target, 0)
        meta["tp_label"] = "HA/MACD_WEAKENING"
        meta["signal_bar"] = signal_bar

        if current_price <= breakout_target:
            logger.debug(
                f"[smrh] {ticker} | BELOW_TARGET "
                f"({current_price:,.0f} <= {breakout_target:,.0f})"
            )
            return BuySignal(
                ticker,
                False,
                current_price,
                f"BELOW_HA_TARGET({current_price:,.0f}<={breakout_target:,.0f})",
                metadata=meta,
            )

        if self._last_entry_bar_keys.get(ticker) == signal_bar:
            reason = f"{_REENTRY_LOCK_REASON}({signal_bar})"
            logger.info(f"[smrh] {ticker} | same signal bar skip | bar={signal_bar}")
            return BuySignal(ticker, False, current_price, reason, metadata=meta)

        cross_tags = []
        if macd_cross:
            cross_tags.append("MACD크로스")
        if stoch_cross:
            cross_tags.append("Stoch크로스")
        cross_str = "+".join(cross_tags) if cross_tags else "NO_CROSS"

        entry_30m_low = float(ha_30m.iloc[-1]["low"])
        self._entry_lows[ticker] = entry_30m_low
        meta["entry_30m_low"] = round(entry_30m_low, 0)

        stop_gap_pct = ((current_price - entry_30m_low) / current_price * 100) if current_price > 0 else 0.0
        meta["entry_stop_gap_pct"] = round(stop_gap_pct, 3)
        if stop_gap_pct > _MAX_ENTRY_STOP_GAP_PCT:
            reason = f"STOP_GAP_TOO_WIDE({stop_gap_pct:.2f}%>{_MAX_ENTRY_STOP_GAP_PCT:.2f}%)"
            logger.info(
                f"[smrh] {ticker} | stop gap skip | "
                f"gap={stop_gap_pct:.2f}% entry_low={entry_30m_low:,.0f}"
            )
            return BuySignal(ticker, False, current_price, reason, metadata=meta)

        engine_stop_pct = min(
            _ENGINE_STOP_MAX_PCT,
            max(stop_gap_pct * 1.05, _ENGINE_STOP_MIN_PCT),
        )
        meta["stop_loss_pct"] = round(engine_stop_pct / 100.0, 6)
        meta["engine_stop_pct"] = round(engine_stop_pct, 3)
        meta["peak_price"] = current_price
        meta["worst_price"] = current_price
        meta["mfe_pct"] = 0.0
        meta["mae_pct"] = 0.0
        self._last_entry_bar_keys[ticker] = signal_bar

        logger.info(
            f"[smrh] ★ 매수신호 | {ticker} | "
            f"현재가={current_price:,.0f} > HA목표={breakout_target:,.0f} | "
            f"4h[MACD Stoch RSI={rsi_4h:.1f} MA20 HA_STRONG] | "
            f"30m[{cross_str} RSI={rsi_30m:.1f}] | "
            f"SL3_low={entry_30m_low:,.0f} | gap={stop_gap_pct:.2f}% | "
            f"engineSL={engine_stop_pct:.2f}%"
        )
        return BuySignal(
            ticker=ticker,
            should_buy=True,
            current_price=current_price,
            reason=f"SMRH_STOP({cross_str})",
            metadata=meta,
        )

    def should_sell_on_signal(
        self, ticker: str, current_price: float, position
    ) -> SellSignal:
        meta = self._update_runtime_stats(position, current_price)
        entry = float(position.buy_price or 0.0)
        pnl_pct = (current_price - entry) / entry * 100 if entry > 0 else 0.0
        peak_pnl_pct = float(meta.get("peak_pnl_pct", pnl_pct) or pnl_pct)
        drop_from_peak_pct = float(meta.get("drop_from_peak_pct", 0.0) or 0.0)
        mfe_pct = float(meta.get("mfe_pct", 0.0) or 0.0)
        mae_pct = float(meta.get("mae_pct", 0.0) or 0.0)

        if peak_pnl_pct >= _BREAKEVEN_TRIGGER_PCT:
            breakeven_floor = entry * (1 + _BREAKEVEN_LOCK_PCT / 100.0)
            position.locked_profit_price = max(
                float(getattr(position, "locked_profit_price", 0.0) or 0.0),
                breakeven_floor,
            )
            meta["breakeven_floor_price"] = round(breakeven_floor, 0)
            if current_price <= breakeven_floor:
                reason = (
                    f"BREAKEVEN_DEFENSE(peak={peak_pnl_pct:+.2f}%"
                    f"|floor={_BREAKEVEN_LOCK_PCT:.1f}%"
                    f"|pnl={pnl_pct:+.2f}%"
                    f"|mfe={mfe_pct:+.2f}%|mae={mae_pct:+.2f}%)"
                )
                logger.info(f"[smrh] 본절방어 청산 | {ticker} | {reason}")
                self._entry_lows.pop(ticker, None)
                return SellSignal(ticker, True, current_price, reason)

        if peak_pnl_pct >= _TRAIL_TRIGGER_PCT and drop_from_peak_pct >= _TRAIL_DROP_PCT:
            reason = (
                f"TRAIL_STOP(peak={peak_pnl_pct:+.2f}%"
                f"|drop={drop_from_peak_pct:.2f}%>={_TRAIL_DROP_PCT:.1f}%"
                f"|pnl={pnl_pct:+.2f}%"
                f"|mfe={mfe_pct:+.2f}%|mae={mae_pct:+.2f}%)"
            )
            logger.info(f"[smrh] 트레일링 청산 | {ticker} | {reason}")
            self._entry_lows.pop(ticker, None)
            return SellSignal(ticker, True, current_price, reason)

        entry_low = self._entry_lows.get(ticker)
        if entry_low is None:
            entry_low = float(meta.get("entry_30m_low", 0.0) or 0.0)
            if entry_low > 0:
                self._entry_lows[ticker] = entry_low
        if entry_low and current_price < entry_low:
            reason = f"HARD_STOP_30M_LOW({self._fmt_price(current_price)}<{self._fmt_price(entry_low)})"
            logger.info(
                f"[smrh] 30m 저가 이탈 청산 | {ticker} | "
                f"pnl={pnl_pct:+.2f}% | MFE={mfe_pct:+.2f}% | "
                f"MAE={mae_pct:+.2f}% | {reason}"
            )
            self._entry_lows.pop(ticker, None)
            return SellSignal(ticker, True, current_price, reason)

        try:
            macd_30m = self._md.compute_macd(
                ticker, _MACD_FAST, _MACD_SLOW, _MACD_SIGNAL, _INTERVAL_30M
            )
            ha_4h = self._md.compute_ha_intraday(ticker, _INTERVAL_4H, count=5)
        except DataFetchError:
            return SellSignal(ticker, False, current_price, "")

        hist = float(macd_30m["hist"])
        hist_prev = float(macd_30m["hist_prev"])
        macd_crossed_negative = hist_prev >= 0 and hist < 0
        macd_weakening_after_profit = (
            hist_prev > 0
            and hist >= 0
            and hist < hist_prev
            and peak_pnl_pct >= _BREAKEVEN_TRIGGER_PCT
        )

        if macd_crossed_negative:
            reason = f"MACD_30M_TURNED_NEG({hist_prev:.4f}->{hist:.4f})"
            logger.info(
                f"[smrh] MACD 약화 청산 | {ticker} | "
                f"MFE={mfe_pct:+.2f}% | MAE={mae_pct:+.2f}% | {reason}"
            )
            self._entry_lows.pop(ticker, None)
            return SellSignal(ticker, True, current_price, reason)

        if macd_weakening_after_profit:
            reason = f"MACD_30M_WEAKENING({hist_prev:.4f}->{hist:.4f})"
            logger.info(
                f"[smrh] MACD 약화 청산 | {ticker} | "
                f"MFE={mfe_pct:+.2f}% | MAE={mae_pct:+.2f}% | {reason}"
            )
            self._entry_lows.pop(ticker, None)
            return SellSignal(ticker, True, current_price, reason)

        ha_last = ha_4h.iloc[-1]
        ha_prev = ha_4h.iloc[-2]
        if bool(ha_prev["is_bullish"]) and not bool(ha_last["is_bullish"]):
            reason = "HA_4H_TURNED_BEAR"
            logger.info(
                f"[smrh] 4H HA 반전 청산 | {ticker} | "
                f"pnl={pnl_pct:+.2f}% | MFE={mfe_pct:+.2f}% | "
                f"MAE={mae_pct:+.2f}% | {reason}"
            )
            self._entry_lows.pop(ticker, None)
            return SellSignal(ticker, True, current_price, reason)

        return SellSignal(ticker, False, current_price, "")
