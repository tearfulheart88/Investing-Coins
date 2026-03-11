"""
전략: 5분봉 5 EMA 극단적 이격 반전 전략 (Long 전용, 개선판 v3)
시나리오 ID: scalping_5ema_reversal

■ 개선 사항 (v3) — 2026-03-05 일보 기반
  - 연속 양봉 확인 필터: 이전 캔들이 양봉이어야 반전 진입 (거짓 반전 방지)
  - 거래량 기준 상향: VOL_MULT 1.5x → 2.0x (진성 반전만 포착)

■ 개선 사항 (v2)
  1. HTF 추세 필터  : 15분봉 EMA(200) 위에서만 진입 (상위 추세 정렬)
  2. ADX 횡보 필터  : ADX(14, 5분봉) ≥ 20 (추세장 확인)
  3. RSI 과매도 조건: RSI(14, 5분봉) < RSI_ENTRY_MAX(40) → 과매도 확인 후 진입
  4. 거래량 급증 조건: 현재 거래량 ≥ Vol_SMA(20) × VOL_MULT(2.0) → 진성 반전 확인
  5. Trailing Stop  : 고정 TP 대신 Trailing Stop 적용
  6. 타임컷 청산    : 진입 후 TIME_CUT_MIN(15분) 경과 + 수익률 < 0.5% → 강제 청산
  7. 연속 양봉 필터 : 이전 캔들 close > open (양봉) → 반전 확인 후 진입

Long 매수 조건:
  1. 15분봉 현재가 > EMA(200, 15m)    [HTF 추세 필터]
  2. ADX(14, 5분봉) ≥ ADX_MIN(20)     [횡보 필터]
  3. RSI(14, 5분봉) < RSI_ENTRY_MAX(40) [과매도 확인]
  4. 현재 거래량 ≥ Vol_SMA × VOL_MULT  [거래량 급증]
  4a. 이전 캔들 양봉 (close > open)    [반전 확인]
  5. 이전 캔들 고가(High) < EMA(5)     [캔들 전체 이평선 아래]
  6. 현재가 > 이전 캔들 고가           [상향 돌파 진입]

청산:
  - Trailing Stop (기존)
  - 타임컷: TIME_CUT_MIN(15분) 경과 + pnl < MIN_MOMENTUM_PCT(0.5%)

타임프레임: 5분봉 (진입) + 15분봉 (HTF 필터)
"""
import logging
from datetime import datetime, timezone, timedelta
from data.market_data import MarketData
from exchange.upbit_client import DataFetchError
from strategies.base_strategy import BaseStrategy, BuySignal, SellSignal

logger = logging.getLogger(__name__)

_INTERVAL        = "minute5"
_HTF_INTERVAL    = "minute15"
_HTF_EMA         = 200
_EMA_P           = 5
_RR              = 3.0    # 손익비 (trailing 활성화 기준 계산에도 사용)
_ADX_MIN         = 20.0
_RSI_ENTRY_MAX   = 40.0   # RSI 진입 최대값 (과매도 확인 — 이 값 미만이어야 진입)
_VOL_MULT        = 2.0    # v3: 1.5x → 2.0x (진성 반전만 포착)
_VOL_SMA_PERIOD  = 20     # 거래량 SMA 기간
_TIME_CUT_MIN    = 15.0   # 타임컷 기준 시간(분) — 5분봉 3캔들 기준
_MIN_MOMENTUM_PCT= 0.5    # 타임컷 최소 수익률 기준 (%)

_KST = timezone(timedelta(hours=9))


class FiveEMAReversalStrategy(BaseStrategy):

    def __init__(self, market_data: MarketData) -> None:
        self._md    = market_data
        self._peaks: dict[str, float] = {}   # ticker → 최고가

    def get_strategy_id(self) -> str:
        return "scalping"

    def get_scenario_id(self) -> str:
        return "scalping_5ema_reversal"

    def requires_scheduled_sell(self) -> bool:
        return False

    def get_history_requirements(self) -> dict[str, int]:
        return {
            _INTERVAL: 60,
            _HTF_INTERVAL: _HTF_EMA,
        }

    # ─── 매수 신호 ────────────────────────────────────────────────────────────

    def should_buy(self, ticker: str, current_price: float) -> BuySignal:
        try:
            ema_df          = self._md.compute_ema_df(ticker, [_EMA_P], _INTERVAL)
            htf_df          = self._md.compute_ema_df(ticker, [_HTF_EMA], _HTF_INTERVAL)
            adx             = self._md.compute_adx(ticker, 14, _INTERVAL)
            rsi             = self._md.compute_rsi_intraday(ticker, 14, _INTERVAL)
            vol_cur, vol_sma = self._md.compute_volume_sma_intraday(ticker, _VOL_SMA_PERIOD, _INTERVAL)
        except DataFetchError as e:
            logger.warning(f"[5ema_reversal] 데이터 오류: {ticker} - {e}")
            return BuySignal(ticker, False, current_price, "DATA_ERROR")

        if len(ema_df) < 3 or len(htf_df) < 1:
            return BuySignal(ticker, False, current_price, "DATA_INSUFFICIENT")

        cur  = ema_df.iloc[-1]
        prev = ema_df.iloc[-2]

        ema5      = float(cur[f"ema{_EMA_P}"])
        ema_htf   = float(htf_df.iloc[-1][f"ema{_HTF_EMA}"])
        prev_high = float(prev["high"])
        prev_low  = float(prev["low"])
        vol_ratio = vol_cur / vol_sma if vol_sma > 0 else 0.0

        meta = {
            "ema5": round(ema5, 0), "prev_high": round(prev_high, 0),
            "prev_low": round(prev_low, 0),
            f"ema{_HTF_EMA}_15m": round(ema_htf, 0), "adx_5m": round(adx, 1),
            "rsi_5m": round(rsi, 1), "vol_ratio": round(vol_ratio, 2),
        }

        # ── 필터 1: HTF 추세 ────────────────────────────────────────────────
        if current_price <= ema_htf:
            return BuySignal(ticker, False, current_price,
                             f"HTF_BELOW_EMA{_HTF_EMA}({current_price:.0f}<={ema_htf:.0f})",
                             metadata=meta)

        # ── 필터 2: ADX ────────────────────────────────────────────────────
        if adx < _ADX_MIN:
            return BuySignal(ticker, False, current_price,
                             f"ADX_WEAK({adx:.1f}<{_ADX_MIN})", metadata=meta)

        # ── 필터 3: RSI 과매도 확인 (RSI < _RSI_ENTRY_MAX) ─────────────────
        if rsi >= _RSI_ENTRY_MAX:
            return BuySignal(ticker, False, current_price,
                             f"RSI_NOT_OVERSOLD({rsi:.1f}>={_RSI_ENTRY_MAX})", metadata=meta)

        # ── 필터 4: 거래량 급증 확인 ────────────────────────────────────────
        if vol_ratio < _VOL_MULT:
            return BuySignal(ticker, False, current_price,
                             f"VOLUME_LOW({vol_ratio:.2f}x<{_VOL_MULT}x)", metadata=meta)

        # ── 필터 4a: 이전 캔들 양봉 확인 (반전 확인) ──────────────────────
        prev_open  = float(prev["open"])
        prev_close = float(prev["close"])
        if prev_close <= prev_open:
            return BuySignal(ticker, False, current_price,
                             "PREV_NOT_BULLISH(close<=open)", metadata=meta)

        # ── 조건 5: 이전 캔들 고가 < EMA5 ──────────────────────────────────
        if prev_high >= ema5:
            return BuySignal(ticker, False, current_price,
                             "NO_VALIDATION_CANDLE", metadata=meta)

        # ── 조건 6: 현재가 > 이전 캔들 고가 ────────────────────────────────
        if current_price <= prev_high:
            return BuySignal(ticker, False, current_price,
                             f"TRIGGER_NOT_MET(need>{prev_high:.0f})", metadata=meta)

        # ── SL / TP 계산 ────────────────────────────────────────────────────
        sl_dist = current_price - prev_low
        if sl_dist <= 0:
            return BuySignal(ticker, False, current_price, "INVALID_SL", metadata=meta)

        sl_pct   = sl_dist / current_price
        tp_price = current_price + sl_dist * _RR

        if sl_pct > 0.10:
            return BuySignal(ticker, False, current_price,
                             f"SL_TOO_WIDE({sl_pct:.1%})", metadata=meta)

        meta.update({
            "sl_pct": round(sl_pct, 6), "tp_price": round(tp_price, 0),
            "stop_loss_pct": round(sl_pct, 6),
            "tp_label": f"RR 1:{_RR:.1f}",
        })

        logger.info(
            f"[5ema_reversal] ★ Long진입 | {ticker} | "
            f"EMA5={ema5:.0f} prev_high={prev_high:.0f} "
            f"SL={prev_low:.0f}({sl_pct:.2%}) TP≈{tp_price:.0f} | "
            f"HTF_EMA{_HTF_EMA}={ema_htf:.0f} ADX={adx:.1f} RSI={rsi:.1f} Vol={vol_ratio:.2f}x"
        )
        return BuySignal(
            ticker=ticker, should_buy=True, current_price=current_price,
            reason="5EMA_REVERSAL_LONG+HTF+ADX+RSI+VOL", metadata=meta,
        )

    # ─── 매도 신호 (Trailing Stop) ────────────────────────────────────────────

    def should_sell_on_signal(self, ticker, current_price, position) -> SellSignal:
        entry    = position.buy_price
        sl_dist  = entry - position.stop_loss_price
        if sl_dist <= 0:
            return SellSignal(ticker, False, current_price, "")

        sl_pct = sl_dist / entry   # 원래 sl 비율

        # 최고가 갱신
        peak = self._peaks.get(ticker, current_price)
        if current_price > peak:
            peak = current_price
        self._peaks[ticker] = peak

        # Trailing 활성화: entry + sl_dist × RR × 0.5
        activation_price = entry + sl_dist * _RR * 0.5

        if peak < activation_price:
            # 미활성: risk_manager 하드 스탑에 위임
            return SellSignal(ticker, False, current_price, "")

        # 활성: trail_stop = max(본절가, peak × (1 − sl_pct))
        trail_stop = max(entry, peak * (1 - sl_pct))

        if current_price <= trail_stop:
            pnl_pct = (current_price - entry) / entry * 100
            reason  = f"TRAIL_STOP({trail_stop:.0f}|peak={peak:.0f})"
            logger.info(
                f"[5ema_reversal] ★ Trailing Stop | {ticker} | "
                f"수익={pnl_pct:+.2f}% | {reason}"
            )
            self._peaks.pop(ticker, None)
            return SellSignal(ticker, True, current_price, reason)

        # ── 타임컷: 진입 후 TIME_CUT_MIN 경과 + 모멘텀 부재 ─────────────────
        try:
            buy_time = position.buy_time
            if isinstance(buy_time, str):
                buy_time = datetime.fromisoformat(buy_time)
            if buy_time.tzinfo is None:
                buy_time = buy_time.replace(tzinfo=_KST)
            elapsed_min = (datetime.now(_KST) - buy_time).total_seconds() / 60

            if elapsed_min >= _TIME_CUT_MIN:
                pnl_pct = (current_price - entry) / entry * 100
                if pnl_pct < _MIN_MOMENTUM_PCT:
                    reason = f"TIME_CUT({elapsed_min:.0f}m|pnl={pnl_pct:+.2f}%<{_MIN_MOMENTUM_PCT}%)"
                    logger.info(f"[5ema_reversal] ⏱ 타임컷 청산 | {ticker} | {reason}")
                    self._peaks.pop(ticker, None)
                    return SellSignal(ticker, True, current_price, reason)
        except Exception as e:
            logger.debug(f"[5ema_reversal] time-cut 계산 오류: {e}")

        return SellSignal(ticker, False, current_price, "")
