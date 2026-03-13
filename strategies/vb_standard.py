"""
전략: 변동성 돌파 표준형 → 동적 K 적용 (개선판 v3)
시나리오 ID: vb_standard

■ 개선 사항 (v3) — 2026-03-05 일보 기반
  - 타임컷 완화: 1h/1% → 2h/0.3% (수익 거래 조기 청산 방지)
  - 본절 방어: peak PnL ≥ 1.0% 도달 시 SL을 진입가+0.2%로 이동
  - 트레일링: peak에서 -0.5% 하락 시 청산

■ 개선 사항 (v2)
  1. 동적 K: 노이즈 필터 K + 클램프 max(0.3, min(0.8, k))
  2. 거래량 확인 필터: 5분봉 현재 거래량 >= Vol_SMA(20) × VOL_MULT(2.5)
  3. datetime 타입 가드

매수 조건:
  1. target = today_open + yesterday_range * k  (k = 동적 노이즈 필터)
     current_price >= target
  2. vol_cur_5m >= vol_sma_5m × VOL_MULT(2.5)  (거래량 급증 확인)

매도 전략:
  TP  : 익일 09:00 KST 스케줄 매도 (requires_scheduled_sell=True)
  SL  : risk_manager 손절
  TC  : 매수 후 2h 경과 + 수익률 < 0.3% → time-cut 청산
  BE  : peak PnL ≥ 1.0% → SL을 진입가+0.2%로 이동
  TSL : peak에서 -0.5% 하락 → 트레일링 청산

임포트 규칙:
  이 파일은 base_strategy, data.market_data 만 임포트.
"""
import logging
from datetime import datetime, timezone, timedelta
from data.market_data import MarketData
from exchange.upbit_client import DataFetchError
from strategies.base_strategy import BaseStrategy, BuySignal, SellSignal

logger = logging.getLogger(__name__)

_NOISE_FILTER_DAYS  = 5      # 동적 K 계산 기간 (일)
_K_MIN              = 0.3    # K 클램프 하한
_K_MAX              = 0.8    # K 클램프 상한
_TIME_CUT_HOURS     = 2.0    # v3: 1h → 2h (수익 거래 조기 청산 방지)
_MIN_MOMENTUM_PCT   = 0.3    # v3: 1.0% → 0.3% (진짜 정체 거래만 청산)
_VOL_MULT           = 2.5    # 거래량 급증 배수 기준 (5분봉 기준)
_VOL_SMA_PERIOD     = 20     # 거래량 SMA 기간
_BE_TRIGGER_PCT     = 1.0    # v3: 본절 방어 활성화 기준 (peak PnL ≥ 이 값%)
_BE_FLOOR_PCT       = 0.2    # v3: 본절 방어 시 최소 수익률(%)
_TRAIL_DROP_PCT     = 0.5    # v3: 트레일링 — peak에서 이만큼(%) 하락 시 청산

_KST = timezone(timedelta(hours=9))


class VBStandardStrategy(BaseStrategy):

    def __init__(self, market_data: MarketData) -> None:
        self._md = market_data
        self._peaks: dict[str, float] = {}  # v3: ticker → 최고가 (본절방어 + 트레일링)

    def get_strategy_id(self) -> str:
        return "volatility_breakout"

    def get_scenario_id(self) -> str:
        return "vb_standard"

    def requires_scheduled_sell(self) -> bool:
        return True

    def get_history_requirements(self) -> dict[str, int]:
        return {
            "day": max(_NOISE_FILTER_DAYS + 1, 3),
            "minute5": _VOL_SMA_PERIOD + 1,
        }

    def get_ticker_selection_profile(self) -> dict:
        return {
            "pattern": "vol_breakout_basic",
            "pool_size": 80,
            "refresh_hours": 0.5,
        }

    def should_buy(self, ticker: str, current_price: float) -> BuySignal:
        try:
            k_raw            = self._md.compute_noise_filter_k(ticker, days=_NOISE_FILTER_DAYS)
            k                = max(_K_MIN, min(_K_MAX, k_raw))   # 동적 K + 클램프
            target_price     = self._md.compute_target_price(ticker, k)
            vol_cur, vol_sma = self._md.compute_volume_sma_intraday(ticker, _VOL_SMA_PERIOD, "minute5")
        except DataFetchError as e:
            logger.warning(f"[vb_standard] 데이터 조회 실패: {ticker}: {e}")
            return BuySignal(ticker, False, current_price, "DATA_ERROR")

        vol_ratio = vol_cur / vol_sma if vol_sma > 0 else 0.0
        breakout  = current_price >= target_price
        vol_ok    = vol_ratio >= _VOL_MULT

        if breakout and vol_ok:
            reason = "BREAKOUT+VOL"
            should = True
        elif breakout and not vol_ok:
            reason = f"BREAKOUT_NO_VOL({vol_ratio:.2f}x<{_VOL_MULT}x)"
            should = False
        else:
            reason = "NO_BREAKOUT"
            should = False

        logger.debug(
            f"[vb_standard] {ticker} | price={current_price:,.0f} "
            f"target={target_price:,.0f} k={k:.3f}(raw={k_raw:.3f}) "
            f"vol={vol_ratio:.2f}x → {reason}"
        )

        return BuySignal(
            ticker=ticker,
            should_buy=should,
            current_price=current_price,
            reason=reason,
            metadata={
                "k": round(k, 4),
                "k_raw": round(k_raw, 4),
                "target_price": round(target_price, 0),
                "vol_ratio": round(vol_ratio, 2),
                "tp_label": "09:00/동적",
            },
        )

    def should_sell_on_signal(self, ticker, current_price, position) -> SellSignal:
        entry = position.buy_price
        pnl_pct = (current_price - entry) / entry * 100

        # ── 최고가 갱신 (본절 방어 + 트레일링용) ─────────────────────────────
        peak = self._peaks.get(ticker, current_price)
        if current_price > peak:
            peak = current_price
        self._peaks[ticker] = peak

        peak_pnl_pct = (peak - entry) / entry * 100

        # ── 트레일링: peak에서 TRAIL_DROP_PCT 이상 하락 시 청산 ────────────
        if peak_pnl_pct >= _BE_TRIGGER_PCT:
            drop_from_peak = (peak - current_price) / peak * 100
            if drop_from_peak >= _TRAIL_DROP_PCT:
                reason = (
                    f"TRAIL_DROP(peak={peak_pnl_pct:+.2f}%"
                    f"|drop={drop_from_peak:.2f}%>={_TRAIL_DROP_PCT}%)"
                )
                logger.info(f"[vb_standard] ★ 트레일링 청산 | {ticker} | pnl={pnl_pct:+.2f}% | {reason}")
                self._peaks.pop(ticker, None)
                return SellSignal(ticker, True, current_price, reason)

        # ── 본절 방어: peak PnL ≥ BE_TRIGGER이면 SL을 진입가+BE_FLOOR로 ──
        if peak_pnl_pct >= _BE_TRIGGER_PCT:
            be_floor_price = entry * (1 + _BE_FLOOR_PCT / 100)
            if current_price <= be_floor_price:
                reason = (
                    f"BREAKEVEN_DEFENSE(peak={peak_pnl_pct:+.2f}%"
                    f"|floor={_BE_FLOOR_PCT}%|pnl={pnl_pct:+.2f}%)"
                )
                logger.info(f"[vb_standard] ★ 본절방어 청산 | {ticker} | {reason}")
                self._peaks.pop(ticker, None)
                return SellSignal(ticker, True, current_price, reason)

        # ── Time-cut: 일정 시간 경과 후 모멘텀 부재 시 청산 ────────────────
        try:
            buy_time = position.buy_time
            if isinstance(buy_time, str):
                buy_time = datetime.fromisoformat(buy_time)
            if buy_time.tzinfo is None:
                buy_time = buy_time.replace(tzinfo=_KST)
            elapsed_hours = (datetime.now(_KST) - buy_time).total_seconds() / 3600

            if elapsed_hours >= _TIME_CUT_HOURS:
                if pnl_pct < _MIN_MOMENTUM_PCT:
                    reason = (
                        f"TIME_CUT({elapsed_hours:.1f}h"
                        f"|pnl={pnl_pct:+.2f}%<{_MIN_MOMENTUM_PCT}%)"
                    )
                    logger.info(
                        f"[vb_standard] ⏱ 타임컷 청산 | {ticker} | {reason}"
                    )
                    self._peaks.pop(ticker, None)
                    return SellSignal(ticker, True, current_price, reason)
        except Exception as e:
            logger.debug(f"[vb_standard] time-cut 계산 오류: {e}")

        return SellSignal(ticker, False, current_price, "")
