"""
전략: RSI 과매도 평균회귀 (개선판 v5)
시나리오 ID: mr_rsi

■ 개선 사항 (v5) — 2026-03-11 Gemini 분석 기반
  - 하드 손절매 추가: 진입가 대비 -7% 즉시 강제 청산 (단일 거래 최대 손실 제한)
  - RSI_BUY 강화: 35 → 30 (과매도 기준 엄격화, 급락장 무분별 진입 방지)
  - RSI_BUY_RANGE 강화: 37 → 32 (약한 횡보장 완화 기준도 엄격화)
  - ADX_RANGE_THR 강화: 20 → 15 (추세장/횡보장 경계 더 엄격하게 구분)

■ 개선 사항 (v4) — 2026-03-10
  - 쿨다운 4시간: 매도 후 동일 종목 4시간 재진입 금지
  - 트레일링 스탑: +1.5% 수익 도달 시 peak 추적, peak에서 1.0% 하락 시 매도

■ 개선 사항 (v3) — 2026-03-05 일보 기반
  - ADX 완화 범위 축소: RSI_BUY_RANGE 40 → 37

■ 개선 사항 (v2)
  1. EMA200(4h) 추세 필터
  2. Dynamic entry: ADX 기반 RSI 완화
  3. Scale-out: RSI 회복 OR 시간 만료

매수 조건:
  1. 현재가 >= EMA(200, 4h)
  2. RSI(14, 1h) <= RSI_BUY (동적: ADX 기반 30 or 32)
  3. 쿨다운 종료 (매도 후 4시간 경과)

매도 조건:
  0. 하드 손절: pnl <= -7%                   (v5 신규 최우선)
  1. 트레일링 스탑: peak >= +1.5% -> -1.0%
  2. RSI >= RSI_SELL(65)
  3. 24h 초과 보유

임포트 규칙:
  이 파일은 base_strategy, data.market_data 만 임포트.
"""
import time
import logging
from datetime import datetime, timezone, timedelta
from data.market_data import MarketData
from exchange.upbit_client import DataFetchError
from strategies.base_strategy import BaseStrategy, BuySignal, SellSignal

logger = logging.getLogger(__name__)

_INTERVAL        = "minute60"
_HTF_INTERVAL    = "minute240"
_HTF_EMA_PERIOD  = 200
_RSI_PERIOD      = 14
_RSI_BUY         = 30.0         # v5: 35->30
_RSI_BUY_RANGE   = 32.0         # v5: 37->32
_RSI_SELL        = 65.0
_ADX_RANGE_THR   = 15.0         # v5: 20->15
_MAX_HOLD_HOURS  = 24.0
_COOLDOWN_HOURS  = 4.0
_TRAIL_TRIGGER_PCT = 1.5
_TRAIL_DROP_PCT    = 1.0
_HARD_SL_PCT       = 7.0        # v5: 신규 — 진입가 대비 -7% 즉시 청산

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

    def on_position_closed(self, ticker: str, reason: str = "") -> None:
        self._peaks.pop(ticker, None)
        cd_end = time.time() + _COOLDOWN_HOURS * 3600
        self._cooldowns[ticker] = cd_end
        logger.info(
            f"[mr_rsi] 쿨다운 등록 | {ticker} | "
            f"{_COOLDOWN_HOURS}h 재진입 금지 | reason={reason}"
        )

    def on_position_reentered(
        self,
        ticker: str,
        new_entry_price: float,
        reason: str = "",
    ) -> None:
        # Re-entry ???곗쟾 peak瑜?湲곗??쇰줈 ?뱀쭠?섎㈃ ?몃젅?쇰쭅 ?좏샇媛 諛섎났 ?깮?꽦???덉쓣 ???덈떎.
        self._peaks[ticker] = new_entry_price
        logger.info(
            f"[mr_rsi] ?ъ쭊???뺣낫 珥덇린??| {ticker} | "
            f"entry={new_entry_price:,.0f} | reason={reason}"
        )

    def should_buy(self, ticker: str, current_price: float) -> BuySignal:
        cd_end = self._cooldowns.get(ticker, 0.0)
        if cd_end > 0:
            now = time.time()
            if now < cd_end:
                remaining_min = (cd_end - now) / 60
                return BuySignal(
                    ticker=ticker, should_buy=False,
                    current_price=current_price,
                    reason=f"COOLDOWN({remaining_min:.0f}min)",
                )
            else:
                del self._cooldowns[ticker]

        try:
            rsi        = self._md.compute_rsi_intraday(ticker, _RSI_PERIOD, _INTERVAL)
            adx        = self._md.compute_adx(ticker, 14, _INTERVAL)
            ema200_4h  = self._md.compute_ema_intraday(ticker, _HTF_EMA_PERIOD, _HTF_INTERVAL)
        except DataFetchError as e:
            logger.warning(f"[mr_rsi] 데이터 조회 실패: {ticker}: {e}")
            return BuySignal(ticker, False, current_price, "DATA_ERROR")

        meta = {
            "rsi_1h":      round(rsi, 2),
            "adx":         round(adx, 1),
            "ema200_4h":   round(ema200_4h, 0),
            "tp_label":    "RSI 회복",
        }

        if current_price < ema200_4h:
            reason = f"BELOW_EMA200_4H({current_price:.0f}<{ema200_4h:.0f})"
            logger.debug(f"[mr_rsi] {ticker} EMA200(4h) 하락 추세 제외 | {reason}")
            return BuySignal(ticker, False, current_price, reason, metadata=meta)

        is_weak_range = adx < _ADX_RANGE_THR
        rsi_buy       = _RSI_BUY_RANGE if is_weak_range else _RSI_BUY
        meta["rsi_buy_thr"] = rsi_buy

        should = rsi <= rsi_buy
        reason = "RSI_OVERSOLD" if should else f"RSI_NORMAL({rsi:.1f})"

        log_fn = logger.info if should else logger.debug
        log_fn(
            f"[mr_rsi] {ticker} | RSI(1h)={rsi:.1f} "
            f"기준={rsi_buy}({'완화' if is_weak_range else '기본'}) "
            f"ADX={adx:.1f} EMA200_4h={ema200_4h:.0f} -> {reason}"
        )

        return BuySignal(
            ticker=ticker,
            should_buy=should,
            current_price=current_price,
            reason=reason,
            metadata=meta,
        )

    def should_sell_on_signal(self, ticker, current_price, position) -> SellSignal:
        entry = position.buy_price
        pnl_pct = (current_price - entry) / entry * 100

        # [v5] 하드 손절매 — 최우선
        if pnl_pct <= -_HARD_SL_PCT:
            reason = f"HARD_SL(pnl={pnl_pct:+.2f}%<=-{_HARD_SL_PCT}%)"
            logger.info(
                f"[mr_rsi] ★ 하드손절 | {ticker} | "
                f"entry={entry:,.0f} now={current_price:,.0f} | {reason}"
            )
            return SellSignal(ticker, True, current_price, reason)

        # 트레일링 스탑
        peak = self._peaks.get(ticker, current_price)
        if current_price > peak:
            peak = current_price
        self._peaks[ticker] = peak
        peak_pnl_pct = (peak - entry) / entry * 100

        if peak_pnl_pct >= _TRAIL_TRIGGER_PCT:
            drop_from_peak = (peak - current_price) / peak * 100
            if drop_from_peak >= _TRAIL_DROP_PCT:
                reason = (
                    f"TRAIL_STOP(peak={peak_pnl_pct:+.2f}%"
                    f"|drop={drop_from_peak:.2f}%>={_TRAIL_DROP_PCT}%"
                    f"|pnl={pnl_pct:+.2f}%)"
                )
                logger.info(
                    f"[mr_rsi] ★ 트레일링 청산 | {ticker} | "
                    f"entry={entry:,.0f} peak={peak:,.0f} now={current_price:,.0f} | {reason}"
                )
                return SellSignal(ticker, True, current_price, reason)

        # RSI 과매수 회복
        try:
            rsi = self._md.compute_rsi_intraday(ticker, _RSI_PERIOD, _INTERVAL)
        except DataFetchError as e:
            logger.warning(f"[mr_rsi] 매도 RSI 조회 실패: {ticker}: {e}")
            return SellSignal(ticker, False, current_price, "")

        if rsi >= _RSI_SELL:
            reason = f"RSI_RECOVERED({rsi:.1f})"
            logger.info(
                f"[mr_rsi] ★ 매도(RSI회복) | {ticker} | "
                f"RSI(1h)={rsi:.1f} >= {_RSI_SELL} | 수익={pnl_pct:+.2f}%"
            )
            return SellSignal(ticker, True, current_price, reason)

        # 최대 보유 시간 초과
        try:
            buy_time = position.buy_time
            if isinstance(buy_time, str):
                buy_time = datetime.fromisoformat(buy_time)
            if buy_time.tzinfo is None:
                buy_time = buy_time.replace(tzinfo=_KST)
            elapsed_hours = (datetime.now(_KST) - buy_time).total_seconds() / 3600
            if elapsed_hours >= _MAX_HOLD_HOURS:
                reason = f"MAX_HOLD_EXPIRED({elapsed_hours:.0f}h|pnl={pnl_pct:+.2f}%)"
                logger.info(f"[mr_rsi] ★ 매도(시간만료) | {ticker} | {reason}")
                return SellSignal(ticker, True, current_price, reason)
        except Exception as e:
            logger.debug(f"[mr_rsi] 시간 계산 오류: {e}")

        return SellSignal(ticker, False, current_price, "")
