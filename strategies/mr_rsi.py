"""
전략: RSI 과매도 평균회귀 (개선판 v4)
시나리오 ID: mr_rsi

■ 개선 사항 (v4) — 2026-03-10
  - 쿨다운 4시간: 매도 후 동일 종목 4시간 재진입 금지
  - 트레일링 스탑: +1.5% 수익 도달 시 peak 추적, peak에서 1.0% 하락 시 매도

■ 개선 사항 (v3) — 2026-03-05 일보 기반
  - ADX 완화 범위 축소: RSI_BUY_RANGE 40 → 37 (RSI 40은 과매도가 아님)

■ 개선 사항 (v2)
  1. EMA200(4h) 추세 필터: 현재가 >= EMA(200, 4h봉) 일 때만 진입
  2. Dynamic entry: ADX < ADX_RANGE_THR(20) (약한 횡보) → RSI_BUY 완화 (35→37)
  3. Scale-out: RSI >= RSI_SELL(65) OR MAX_HOLD_HOURS(24h) 경과

매수 조건:
  1. 현재가 >= EMA(200, 4h)                  (상위 추세 필터)
  2. RSI(14, 1h) <= RSI_BUY (동적: ADX 기반 35 or 37)
  3. 쿨다운 종료 (매도 후 4시간 경과)          (v4 신규)

매도 조건:
  1. 트레일링 스탑: peak ≥ entry+1.5% → peak에서 1.0% 하락 (v4 신규)
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

_INTERVAL        = "minute60"   # 1시간봉
_HTF_INTERVAL    = "minute240"  # 4시간봉 (EMA200 추세 필터용)
_HTF_EMA_PERIOD  = 200          # EMA200 4h 추세 필터
_RSI_PERIOD      = 14
_RSI_BUY         = 35.0         # 기본 매수 기준 (추세장 또는 ADX 높을 때)
_RSI_BUY_RANGE   = 37.0         # v3: 40→37 약한 횡보장 완화 기준 (RSI 40은 과매도 아님)
_RSI_SELL        = 65.0         # 과매수 회복 매도 기준
_ADX_RANGE_THR   = 20.0         # 이 ADX 미만이면 완화 매수 기준 적용
_MAX_HOLD_HOURS  = 24.0         # 최대 보유 시간 (기회비용 제한)
_COOLDOWN_HOURS  = 4.0          # v4: 매도 후 재진입 금지 시간
_TRAIL_TRIGGER_PCT = 1.5        # v4: 트레일링 활성화 수익률 (%)
_TRAIL_DROP_PCT    = 1.0        # v4: peak에서 이만큼 하락 시 매도 (%)

_KST = timezone(timedelta(hours=9))


class RSIStrategy(BaseStrategy):

    def __init__(self, market_data: MarketData) -> None:
        self._md = market_data
        # v4: 쿨다운 — ticker → cooldown_end_time (time.time 기준)
        self._cooldowns: dict[str, float] = {}
        # v4: 트레일링 스탑 — ticker → peak price
        self._peaks: dict[str, float] = {}

    def get_strategy_id(self) -> str:
        return "mean_reversion"

    def get_scenario_id(self) -> str:
        return "mr_rsi"

    def requires_scheduled_sell(self) -> bool:
        return False   # 자체 신호로 청산 → 동일 종목 복수 매매 가능

    def on_position_closed(self, ticker: str, reason: str = "") -> None:
        """v4: 모든 매도 후 쿨다운 등록 + peak 정리."""
        self._peaks.pop(ticker, None)
        cd_end = time.time() + _COOLDOWN_HOURS * 3600
        self._cooldowns[ticker] = cd_end
        logger.info(
            f"[mr_rsi] 쿨다운 등록 | {ticker} | "
            f"{_COOLDOWN_HOURS}h 재진입 금지 | reason={reason}"
        )

    # ─── 매수 신호 ────────────────────────────────────────────────────────────

    def should_buy(self, ticker: str, current_price: float) -> BuySignal:
        # ── [v4] 쿨다운 체크 ──────────────────────────────────────────────────
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
        }

        # EMA200(4h) 추세 필터 — 하락 추세에서 평균회귀 진입 금지
        if current_price < ema200_4h:
            reason = f"BELOW_EMA200_4H({current_price:.0f}<{ema200_4h:.0f})"
            logger.debug(f"[mr_rsi] {ticker} EMA200(4h) 하락 추세 제외 | {reason}")
            return BuySignal(ticker, False, current_price, reason, metadata=meta)

        # Dynamic entry: 약한 횡보장에서는 RSI 기준 완화
        is_weak_range = adx < _ADX_RANGE_THR
        rsi_buy       = _RSI_BUY_RANGE if is_weak_range else _RSI_BUY
        meta["rsi_buy_thr"] = rsi_buy

        should = rsi <= rsi_buy
        reason = "RSI_OVERSOLD" if should else f"RSI_NORMAL({rsi:.1f})"

        # 매수 신호 없는 평가는 DEBUG (매번 반복 → 로그 폭발 방지)
        log_fn = logger.info if should else logger.debug
        log_fn(
            f"[mr_rsi] {ticker} | RSI(1h)={rsi:.1f} "
            f"기준={rsi_buy}({'완화' if is_weak_range else '기본'}) "
            f"ADX={adx:.1f} EMA200_4h={ema200_4h:.0f} → {reason}"
        )

        return BuySignal(
            ticker=ticker,
            should_buy=should,
            current_price=current_price,
            reason=reason,
            metadata=meta,
        )

    # ─── 매도 신호 (Scale-out) ────────────────────────────────────────────────

    def should_sell_on_signal(self, ticker, current_price, position) -> SellSignal:
        entry = position.buy_price
        pnl_pct = (current_price - entry) / entry * 100

        # ── [v4] 최고가 갱신 (트레일링 스탑용) ──────────────────────────────
        peak = self._peaks.get(ticker, current_price)
        if current_price > peak:
            peak = current_price
        self._peaks[ticker] = peak
        peak_pnl_pct = (peak - entry) / entry * 100

        # ── [v4] 트레일링 스탑: peak ≥ +1.5% → peak에서 1.0% 하락 시 매도 ──
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

        # ── Scale-out 1: RSI 과매수 회복 (주요 목표) ─────────────────────────
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

        # ── Scale-out 2: 최대 보유 시간 초과 ────────────────────────────────
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
