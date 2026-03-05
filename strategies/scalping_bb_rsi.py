"""
전략: 15분봉 볼린저 밴드 + RSI 평균 회귀 (ADX 횡보 필터)
시나리오 ID: scalping_bb_rsi

사전 필터: ADX(14, 15m) < 25  (추세 없는 횡보장에서만 매매)
  (크립토는 추세장에서도 ADX 25~40대 → 기존 20 기준은 너무 엄격, 25로 완화)

Long 매수 조건 (AND):
  1. ADX < 25                         [횡보장 확인]
  2. 이전 캔들 저가 < BB 하단(20, 2σ) [하단 이탈]
  3. RSI(14, 15m) < 30                [과매도]
  4. 현재 캔들이 양봉                   [회복 시작 확인 → 진입]

매도 조건:
  가격 >= BB 중심선(20 SMA) → 전량 청산
  SL  : ATR × 1.2 동적 손절 (metadata["stop_loss_pct"] 로 전달)

타임프레임: 15분봉
임포트 규칙: base_strategy, data.market_data 만 임포트.
"""
import logging
from data.market_data import MarketData
from exchange.upbit_client import DataFetchError
from strategies.base_strategy import BaseStrategy, BuySignal, SellSignal
import config

logger = logging.getLogger(__name__)

_INTERVAL   = "minute15"
_BB_PERIOD  = 20
_BB_STD     = 2.0
_RSI_PERIOD = 14
_RSI_BUY    = 30.0
_ADX_PERIOD = 14
_ADX_LIMIT  = 25.0   # 크립토 특성상 20은 너무 엄격 → 25로 완화
_ATR_MULT   = 1.2    # SL = ATR × 1.2
_MAX_SL_PCT = 0.03   # 최대 손절 3%


class ScalpingBBRSIStrategy(BaseStrategy):

    def __init__(self, market_data: MarketData) -> None:
        self._md = market_data

    def get_strategy_id(self) -> str:
        return "scalping"

    def get_scenario_id(self) -> str:
        return "scalping_bb_rsi"

    def requires_scheduled_sell(self) -> bool:
        return False   # 자체 신호로 청산

    def should_buy(self, ticker: str, current_price: float) -> BuySignal:
        try:
            adx              = self._md.compute_adx(ticker, _ADX_PERIOD, _INTERVAL)
            upper, mid, lower = self._md.compute_bollinger_intraday(ticker, _BB_PERIOD, _BB_STD, _INTERVAL)
            rsi              = self._md.compute_rsi_intraday(ticker, _RSI_PERIOD, _INTERVAL)
            raw_df           = self._md.get_ohlcv_intraday(ticker, _INTERVAL, count=_BB_PERIOD + 10)
        except DataFetchError as e:
            logger.warning(f"[scalping_bb_rsi] 데이터 오류: {ticker} - {e}")
            return BuySignal(ticker, False, current_price, "DATA_ERROR")

        meta = {
            "adx":       round(adx, 1),
            "rsi_15m":   round(rsi, 1),
            "bb_upper":  round(upper, 0),
            "bb_middle": round(mid,   0),
            "bb_lower":  round(lower, 0),
        }

        # ── 1. 횡보장 필터 ──
        if adx >= _ADX_LIMIT:
            logger.info(f"[scalping_bb_rsi] {ticker} 추세장 제외 ADX={adx:.1f} >= {_ADX_LIMIT}")
            return BuySignal(ticker, False, current_price, f"TRENDING(ADX={adx:.1f})", metadata=meta)

        if len(raw_df) < 2:
            return BuySignal(ticker, False, current_price, "DATA_INSUFFICIENT", metadata=meta)

        # ── 2. 이전 캔들 저가 < BB 하단 ──
        prev_low = float(raw_df["low"].iloc[-2])
        if prev_low >= lower:
            return BuySignal(ticker, False, current_price, "BB_NOT_BREACHED", metadata=meta)

        # ── 3. RSI 과매도 ──
        if rsi >= _RSI_BUY:
            return BuySignal(ticker, False, current_price, f"RSI_NOT_OVERSOLD({rsi:.1f})", metadata=meta)

        # ── 4. 현재 캔들 양봉 ──
        cur_open  = float(raw_df["open"].iloc[-1])
        cur_close = float(raw_df["close"].iloc[-1])
        if cur_close < cur_open:
            logger.info(f"[scalping_bb_rsi] {ticker} 양봉 대기 (BB하단={lower:.0f} RSI={rsi:.1f})")
            return BuySignal(ticker, False, current_price, "WAITING_BULLISH_CANDLE", metadata=meta)

        # ── ATR 기반 동적 손절 ──
        try:
            atr    = self._md.compute_atr(ticker, period=14, interval=_INTERVAL)
            sl_pct = min(atr / current_price * _ATR_MULT, _MAX_SL_PCT)
        except DataFetchError:
            sl_pct = config.STOP_LOSS_PCT

        meta["stop_loss_pct"] = round(sl_pct, 6)

        logger.info(
            f"[scalping_bb_rsi] 매수 신호 | {ticker} | "
            f"ADX={adx:.1f} RSI={rsi:.1f} BB하단={lower:.0f} SL={sl_pct:.2%}"
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
            _, mid, _ = self._md.compute_bollinger_intraday(ticker, _BB_PERIOD, _BB_STD, _INTERVAL)
        except DataFetchError:
            return SellSignal(ticker, False, current_price, "")

        if current_price >= mid:
            logger.info(
                f"[scalping_bb_rsi] 중심선 도달 매도 | {ticker} | "
                f"price={current_price:.0f} >= mid={mid:.0f}"
            )
            return SellSignal(ticker, True, current_price, f"BB_MIDDLE_REACHED({mid:.0f})")
        return SellSignal(ticker, False, current_price, "")
