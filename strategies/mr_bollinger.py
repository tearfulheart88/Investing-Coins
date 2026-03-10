"""
전략: 볼린저 밴드 + RSI 평균 회귀 (개선판 v2)
시나리오 ID: mr_bollinger

■ 개선 사항 (v2)
  1. Dynamic banding: ADX < ADX_RANGE_THR(20) (약한 횡보) → BB std 축소 (2.0→1.5)
  2. EMA200(4h) 추세 필터: 현재가 >= EMA(200, 4h봉) 일 때만 진입
     하락 추세에서의 평균회귀 진입 방지 (낙칼 잡기 억제)
  3. Scale-out 청산: 다음 중 하나 충족 시 전량 청산
     a. 가격 >= BB 중심선  b. RSI >= 50  c. MAX_HOLD_HOURS(48h) 경과

사전 필터: ADX(14, 1h) < ADX_LIMIT(25)  (추세 없는 횡보장에서만 작동)

매수 조건 (AND):
  1. 현재가 >= EMA(200, 4h)   (상위 추세 필터)
  2. 이전 캔들 저가 < BB 하단  (하단 이탈)
  3. RSI(14, 1h) < RSI_BUY(35) (과매도 확인)
  4. 현재 캔들이 양봉           (회복 시작 확인)

임포트 규칙:
  이 파일은 base_strategy, data.market_data 만 임포트.
"""
import logging
from datetime import datetime, timezone, timedelta
from data.market_data import MarketData
from exchange.upbit_client import DataFetchError
from strategies.base_strategy import BaseStrategy, BuySignal, SellSignal

logger = logging.getLogger(__name__)

_INTERVAL        = "minute60"   # 1시간봉
_HTF_INTERVAL    = "minute240"  # 4시간봉 (EMA200 추세 필터용)
_HTF_EMA_PERIOD  = 200          # EMA200 4h 추세 필터
_BB_PERIOD       = 20
_BB_STD_TREND    = 2.0          # 추세장 BB 표준편차 (ADX >= ADX_RANGE_THR)
_BB_STD_RANGE    = 1.5          # 약한 횡보장 BB 표준편차 (ADX < ADX_RANGE_THR) — 더 좁은 밴드
_ADX_RANGE_THR   = 20.0         # 이 ADX 미만이면 dynamic banding 적용
_RSI_PERIOD      = 14
_RSI_BUY         = 35.0
_ADX_PERIOD      = 14
_ADX_LIMIT       = 25.0         # ADX < 25 → 횡보장으로 진입 허용
_MAX_HOLD_HOURS  = 48.0         # 최대 보유 시간 (scale-out 시간 제한)

_KST = timezone(timedelta(hours=9))


class BollingerStrategy(BaseStrategy):

    def __init__(self, market_data: MarketData) -> None:
        self._md = market_data

    def get_strategy_id(self) -> str:
        return "mean_reversion"

    def get_scenario_id(self) -> str:
        return "mr_bollinger"

    def requires_scheduled_sell(self) -> bool:
        return False   # 자체 신호로 청산

    # ─── 매수 신호 ────────────────────────────────────────────────────────────

    def should_buy(self, ticker: str, current_price: float) -> BuySignal:
        try:
            adx    = self._md.compute_adx(ticker, _ADX_PERIOD, _INTERVAL)
            # Dynamic banding: 횡보 강도에 따라 BB std 조정
            bb_std = _BB_STD_RANGE if adx < _ADX_RANGE_THR else _BB_STD_TREND
            upper, middle, lower = self._md.compute_bollinger_intraday(
                ticker, _BB_PERIOD, bb_std, _INTERVAL
            )
            rsi      = self._md.compute_rsi_intraday(ticker, _RSI_PERIOD, _INTERVAL)
            raw_df   = self._md.get_ohlcv_intraday(ticker, _INTERVAL, count=_BB_PERIOD + 10)
            ema200_4h = self._md.compute_ema_intraday(ticker, _HTF_EMA_PERIOD, _HTF_INTERVAL)
        except DataFetchError as e:
            logger.warning(f"[mr_bollinger] 데이터 조회 실패: {ticker}: {e}")
            return BuySignal(ticker, False, current_price, "DATA_ERROR")

        meta = {
            "adx":         round(adx, 1),
            "bb_std":      bb_std,
            "rsi_1h":      round(rsi, 1),
            "bb_upper":    round(upper, 0),
            "bb_middle":   round(middle, 0),
            "bb_lower":    round(lower, 0),
            "ema200_4h":   round(ema200_4h, 0),
        }

        # 0. EMA200(4h) 추세 필터 — 하락 추세에서 평균회귀 진입 금지
        if current_price < ema200_4h:
            logger.debug(
                f"[mr_bollinger] {ticker} EMA200(4h) 하락 추세 제외 "
                f"price={current_price:.0f} < ema200_4h={ema200_4h:.0f}"
            )
            return BuySignal(
                ticker, False, current_price,
                f"BELOW_EMA200_4H({current_price:.0f}<{ema200_4h:.0f})", metadata=meta
            )

        # 1. 횡보장 필터 (추세장에서는 평균 회귀 위험)
        if adx >= _ADX_LIMIT:
            logger.debug(f"[mr_bollinger] {ticker} 추세장 제외 ADX={adx:.1f} >= {_ADX_LIMIT}")
            return BuySignal(
                ticker, False, current_price, f"TRENDING(ADX={adx:.1f})", metadata=meta
            )

        # 2. 이전 캔들 저가 < BB 하단 (하단 이탈 확인)
        if len(raw_df) < 2:
            return BuySignal(ticker, False, current_price, "DATA_INSUFFICIENT", metadata=meta)
        prev_low = float(raw_df["low"].iloc[-2])
        if prev_low >= lower:
            return BuySignal(ticker, False, current_price, "BB_NOT_TOUCHED", metadata=meta)

        # 3. RSI 과매도 확인
        if rsi >= _RSI_BUY:
            return BuySignal(
                ticker, False, current_price,
                f"RSI_NOT_OVERSOLD({rsi:.1f})", metadata=meta
            )

        # 4. 현재 캔들이 양봉 (회복 시작 확인)
        cur_open  = float(raw_df["open"].iloc[-1])
        cur_close = float(raw_df["close"].iloc[-1])
        if cur_close < cur_open:
            logger.debug(
                f"[mr_bollinger] {ticker} 양봉 대기 중 "
                f"(BB하단={lower:.0f} RSI={rsi:.1f} std={bb_std})"
            )
            return BuySignal(
                ticker, False, current_price, "WAITING_BULLISH_CANDLE", metadata=meta
            )

        logger.info(
            f"[mr_bollinger] ★ 매수 신호 | {ticker} | "
            f"ADX={adx:.1f} RSI={rsi:.1f} BB하단={lower:.0f}(std={bb_std}) → {current_price:.0f}"
        )
        return BuySignal(
            ticker=ticker,
            should_buy=True,
            current_price=current_price,
            reason=f"BB_LOWER+RSI_OVERSOLD+BULLISH(std={bb_std})",
            metadata=meta,
        )

    # ─── 매도 신호 (Scale-out) ────────────────────────────────────────────────

    def should_sell_on_signal(self, ticker, current_price, position) -> SellSignal:
        try:
            adx    = self._md.compute_adx(ticker, _ADX_PERIOD, _INTERVAL)
            bb_std = _BB_STD_RANGE if adx < _ADX_RANGE_THR else _BB_STD_TREND
            _, middle, _ = self._md.compute_bollinger_intraday(
                ticker, _BB_PERIOD, bb_std, _INTERVAL
            )
            rsi = self._md.compute_rsi_intraday(ticker, _RSI_PERIOD, _INTERVAL)
        except DataFetchError as e:
            logger.warning(f"[mr_bollinger] 매도 데이터 조회 실패: {ticker}: {e}")
            return SellSignal(ticker, False, current_price, "")

        pnl_pct = (current_price - position.buy_price) / position.buy_price * 100

        # ── Scale-out 1: BB 중심선 도달 (주요 목표) ─────────────────────────
        if current_price >= middle:
            reason = f"BB_MIDDLE_REACHED({middle:.0f})"
            logger.info(
                f"[mr_bollinger] ★ 매도(중심선) | {ticker} | "
                f"price={current_price:.0f} >= middle={middle:.0f} | 수익={pnl_pct:+.2f}%"
            )
            return SellSignal(ticker, True, current_price, reason)

        # ── Scale-out 2: RSI 50선 회복 (조기 수익 실현) ─────────────────────
        if rsi >= 50.0:
            reason = f"RSI_RECOVERED_50({rsi:.1f})"
            logger.info(
                f"[mr_bollinger] ★ 매도(RSI50) | {ticker} | "
                f"RSI={rsi:.1f} 수익={pnl_pct:+.2f}%"
            )
            return SellSignal(ticker, True, current_price, reason)

        # ── Scale-out 3: 최대 보유 시간 초과 ────────────────────────────────
        try:
            buy_time = datetime.fromisoformat(position.buy_time)
            if buy_time.tzinfo is None:
                buy_time = buy_time.replace(tzinfo=_KST)
            elapsed_hours = (datetime.now(_KST) - buy_time).total_seconds() / 3600

            if elapsed_hours >= _MAX_HOLD_HOURS:
                reason = f"MAX_HOLD_EXPIRED({elapsed_hours:.0f}h|pnl={pnl_pct:+.2f}%)"
                logger.info(f"[mr_bollinger] ★ 매도(시간만료) | {ticker} | {reason}")
                return SellSignal(ticker, True, current_price, reason)
        except Exception as e:
            logger.debug(f"[mr_bollinger] 시간 계산 오류: {e}")

        return SellSignal(ticker, False, current_price, "")
