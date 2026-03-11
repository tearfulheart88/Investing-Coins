"""
전략: MACD 골든크로스 + RSI 추세 추종 (개선판 v3)
시나리오 ID: macd_rsi_trend

■ 개선 사항 (v3) — 2026-03-05 일보 기반
  - 제로라인 아래 조건 삭제: 0건 거래 → "골든크로스+제로하" 동시 충족이 극히 드묾
    순수 골든크로스만으로 진입 (제로라인 위 골든크로스도 유효한 추세 전환 시그널)
  - RSI 기준 완화: 55 → 50 (더 많은 기회 포착)

■ 개선 사항 (v2)
  1. MACD 라인 골든크로스 기반 진입 (히스토그램 대비 빠름)
  2. 순수 데드크로스 청산 (추세 완전 반전 시까지 보유)

■ 매수 조건 (AND 4가지)
  1. MACD 골든크로스:
     macd_prev < signal_prev AND macd_now >= signal_now
  2. RSI > RSI_ENTRY_MIN(50)         — 가짜 반등 필터
  3. RSI 상승 중 (현재 RSI > 이전 RSI)
  4. 현재 봉 거래량 >= Vol_SMA(20) × VOL_MULT(1.5)  — 거래량 급증 확인

■ 매도 조건
  SL1 : RSI < RSI_SL(45)                              (추세 약화)
  TP  : MACD 데드크로스 (macd_prev > signal_prev AND macd_now <= signal_now)

타임프레임: 1시간봉 (minute60)
"""
import logging
from data.market_data import MarketData
from exchange.upbit_client import DataFetchError
from strategies.base_strategy import BaseStrategy, BuySignal, SellSignal

logger = logging.getLogger(__name__)

_INTERVAL       = "minute60"
_RSI_PERIOD     = 14
_RSI_ENTRY_MIN  = 50.0        # v3: 55→50 최소 진입 RSI 기준 (더 많은 기회 포착)
_RSI_SL         = 45.0        # RSI 손절 기준 (추세 약화)
_MACD_FAST      = 12
_MACD_SLOW      = 26
_MACD_SIGNAL    = 9
_VOL_SMA_PERIOD = 20
_VOL_MULT       = 1.5         # 거래량 급증 배수 기준


class MACDRSITrendStrategy(BaseStrategy):

    def __init__(self, market_data: MarketData) -> None:
        self._md = market_data

    def get_strategy_id(self) -> str:
        return "trend_following"

    def get_scenario_id(self) -> str:
        return "macd_rsi_trend"

    def requires_scheduled_sell(self) -> bool:
        return False

    def get_history_requirements(self) -> dict[str, int]:
        return {
            _INTERVAL: max(
                _MACD_SLOW + _MACD_SIGNAL + 3,
                _RSI_PERIOD * 4 + 2,
                _VOL_SMA_PERIOD + 5,
            ),
        }

    # ─── 매수 신호 ────────────────────────────────────────────────────────────

    def should_buy(self, ticker: str, current_price: float) -> BuySignal:
        try:
            macd             = self._md.compute_macd(ticker, _MACD_FAST, _MACD_SLOW, _MACD_SIGNAL, _INTERVAL)
            rsi_series       = self._md.compute_rsi_series_intraday(ticker, _RSI_PERIOD, _INTERVAL, n=2)
            vol_cur, vol_sma = self._md.compute_volume_sma_intraday(ticker, _VOL_SMA_PERIOD, _INTERVAL)
        except DataFetchError as e:
            logger.warning(f"[macd_rsi] 데이터 오류: {ticker} - {e}")
            return BuySignal(ticker, False, current_price, "DATA_ERROR")

        rsi_now   = rsi_series[-1]
        rsi_prev  = rsi_series[-2]
        macd_now  = macd["macd"]
        sig_now   = macd["signal_val"]
        macd_prev = macd["macd_prev"]
        sig_prev  = macd["signal_prev"]
        vol_ratio = vol_cur / vol_sma if vol_sma > 0 else 0.0

        meta = {
            "macd_now":   round(macd_now,  6),
            "macd_prev":  round(macd_prev, 6),
            "sig_now":    round(sig_now,   6),
            "sig_prev":   round(sig_prev,  6),
            "rsi_1h":     round(rsi_now,   1),
            "rsi_prev":   round(rsi_prev,  1),
            "vol_ratio":  round(vol_ratio, 2),
            "tp_label":   "MACD/RSI 약화",
        }

        # ── 조건 1: MACD 골든크로스 ──────────────────────────────────────────
        # v3: 제로라인 아래 조건 삭제 (0건 거래 → 조건이 너무 제한적)
        # macd_prev < signal_prev (직전: MACD가 시그널 아래)
        # macd_now >= signal_now  (현재: MACD가 시그널 상향 돌파)
        golden_cross = (macd_prev < sig_prev) and (macd_now >= sig_now)
        if not golden_cross:
            if macd_now >= sig_now and macd_prev >= sig_prev:
                reason = "MACD_ALREADY_ABOVE_SIGNAL"
            else:
                reason = f"MACD_NO_GOLDEN_CROSS(m={macd_now:.5f},s={sig_now:.5f})"
            logger.debug(f"[macd_rsi] {ticker} | {reason}")
            return BuySignal(ticker, False, current_price, reason, metadata=meta)

        # ── 조건 2: RSI > RSI_ENTRY_MIN ───────────────────────────────────
        if rsi_now < _RSI_ENTRY_MIN:
            reason = f"RSI_WEAK({rsi_now:.1f}<{_RSI_ENTRY_MIN})"
            logger.info(f"[macd_rsi] {ticker} RSI 미달 | RSI={rsi_now:.1f} 기준={_RSI_ENTRY_MIN}")
            return BuySignal(ticker, False, current_price, reason, metadata=meta)

        # ── 조건 3: RSI 상승 중 ───────────────────────────────────────────
        if rsi_now <= rsi_prev:
            reason = f"RSI_NOT_RISING({rsi_now:.1f}<={rsi_prev:.1f})"
            logger.info(f"[macd_rsi] {ticker} RSI 하락 | RSI={rsi_now:.1f} prev={rsi_prev:.1f}")
            return BuySignal(ticker, False, current_price, reason, metadata=meta)

        # ── 조건 4: 거래량 급증 확인 ─────────────────────────────────────
        if vol_ratio < _VOL_MULT:
            reason = f"VOLUME_LOW({vol_ratio:.2f}x<{_VOL_MULT}x)"
            logger.info(f"[macd_rsi] {ticker} 거래량 부족 | vol_ratio={vol_ratio:.2f}x")
            return BuySignal(ticker, False, current_price, reason, metadata=meta)

        # ── 전체 조건 충족 → 매수 ────────────────────────────────────────
        logger.info(
            f"[macd_rsi] ★ 매수신호 | {ticker} | "
            f"MACD 골든크로스 m:{macd_prev:.5f}→{macd_now:.5f} s:{sig_now:.5f} | "
            f"RSI(1h)={rsi_now:.1f}↑(prev={rsi_prev:.1f}) | "
            f"Vol={vol_ratio:.2f}x"
        )
        return BuySignal(
            ticker=ticker, should_buy=True, current_price=current_price,
            reason="MACD_GOLDEN_CROSS+RSI+VOL", metadata=meta,
        )

    # ─── 매도 신호 ────────────────────────────────────────────────────────────

    def should_sell_on_signal(self, ticker, current_price, position) -> SellSignal:
        try:
            macd       = self._md.compute_macd(ticker, _MACD_FAST, _MACD_SLOW, _MACD_SIGNAL, _INTERVAL)
            rsi_series = self._md.compute_rsi_series_intraday(ticker, _RSI_PERIOD, _INTERVAL, n=2)
        except DataFetchError:
            return SellSignal(ticker, False, current_price, "")

        rsi_now   = rsi_series[-1]
        macd_now  = macd["macd"]
        sig_now   = macd["signal_val"]
        macd_prev = macd["macd_prev"]
        sig_prev  = macd["signal_prev"]

        # ── SL1: RSI 이탈 (추세 약화) ─────────────────────────────────────
        if rsi_now < _RSI_SL:
            reason = f"RSI_SL({rsi_now:.1f}<{_RSI_SL})"
            logger.info(f"[macd_rsi] 손절(RSI) | {ticker} | {reason}")
            return SellSignal(ticker, True, current_price, reason)

        # ── TP: MACD 데드크로스 (추세 완전 반전) ─────────────────────────
        # macd_prev > signal_prev (직전: MACD가 시그널 위에 있었음)
        # macd_now <= signal_now  (현재: MACD가 시그널 하향 돌파)
        dead_cross = (macd_prev > sig_prev) and (macd_now <= sig_now)
        if dead_cross:
            pnl_pct = (current_price - position.buy_price) / position.buy_price * 100
            reason = f"MACD_DEAD_CROSS(m:{macd_prev:.5f}→{macd_now:.5f},s:{sig_now:.5f})"
            logger.info(
                f"[macd_rsi] ★ 청산(데드크로스) | {ticker} | "
                f"수익={pnl_pct:+.2f}% | {reason}"
            )
            return SellSignal(ticker, True, current_price, reason)

        return SellSignal(ticker, False, current_price, "")
