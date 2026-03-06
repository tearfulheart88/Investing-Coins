"""
전략: 변동성 돌파 + 노이즈 필터 + 이동평균선 필터 (개선판 v3)
시나리오 ID: vb_noise_filter

■ 개선 사항 (v3) — 2026-03-05 일보 기반
  - 타임컷 완화: 1h/1% → 2h/0.3% (수익 거래 조기 청산 방지)
    일보에서 +0.5~0.9% 거래가 TIME_CUT에 의해 잘려나감 → 기준 대폭 완화
  - 본절 방어: peak PnL ≥ 1.0% 도달 시 SL을 진입가+0.2%로 이동
    수익 반납 방지 (peak에서 되돌릴 때 최소 수익 확보)
  - 트레일링: peak에서 -0.5% 하락 시 청산
    본절 방어보다 높은 수준의 수익 보호

■ 개선 사항 (v2)
  1. K 클램프 강화: max(K_MIN=0.3, min(K_MAX=0.8, k))
  2. 거래량 확인 필터: 5분봉 현재 거래량 >= Vol_SMA(20) × VOL_MULT(2.5)
  3. datetime 타입 가드: position.buy_time이 str 또는 datetime 모두 처리

매수 조건 (AND):
  1. current_price >= today_open + yesterday_range * k  (변동성 돌파)
  2. current_price > MA(15)                             (상승 추세 확인)
  3. vol_cur_5m >= vol_sma_5m × VOL_MULT(2.5)          (거래량 급증 확인)

매도 전략:
  TP  : 익일 09:00 KST 스케줄 매도 (requires_scheduled_sell=True)
  SL  : risk_manager 손절
  TC  : 매수 후 2h 경과 + 수익률 < 0.3% → time-cut 청산
  BE  : peak PnL ≥ 1.0% → SL을 진입가+0.2%로 이동 (본절 방어)
  TSL : peak에서 -0.5% 하락 → 트레일링 청산

임포트 규칙:
  이 파일은 base_strategy, data.market_data 만 임포트.
  다른 전략 파일 임포트 금지.
"""
import logging
from datetime import datetime, timezone, timedelta
from data.market_data import MarketData
from exchange.upbit_client import DataFetchError
from strategies.base_strategy import BaseStrategy, BuySignal, SellSignal
import config

logger = logging.getLogger(__name__)

_K_MIN              = 0.3    # K 클램프 하한 (너무 공격적 돌파 방지)
_K_MAX              = 0.8    # K 클램프 상한 (너무 보수적 돌파 방지)
_TIME_CUT_HOURS     = 2.0    # v3: 1h → 2h (수익 거래 조기 청산 방지)
_MIN_MOMENTUM_PCT   = 0.3    # v3: 1.0% → 0.3% (진짜 정체 거래만 청산)
_VOL_MULT           = 2.5    # 거래량 급증 배수 기준 (5분봉 기준)
_VOL_SMA_PERIOD     = 20     # 거래량 SMA 기간
_BE_TRIGGER_PCT     = 1.0    # v3: 본절 방어 활성화 기준 (peak PnL ≥ 이 값%)
_BE_FLOOR_PCT       = 0.2    # v3: 본절 방어 시 최소 수익률(%) (SL → entry × (1+0.002))
_TRAIL_DROP_PCT     = 0.5    # v3: 트레일링 — peak에서 이만큼(%) 하락 시 청산

_KST = timezone(timedelta(hours=9))


class VBNoiseFilterStrategy(BaseStrategy):

    def __init__(self, market_data: MarketData) -> None:
        self._md = market_data
        self._peaks: dict[str, float] = {}        # v3: ticker → 최고가 (본절방어 + 트레일링)
        # [개선] 타임컷 1회 연장 사용 여부 추적 (모멘텀 유지 시 1회만 바이패스 허용)
        self._time_cut_extended: set[str] = set() # ticker → 이미 연장 사용함

    def get_strategy_id(self) -> str:
        return "volatility_breakout"

    def get_scenario_id(self) -> str:
        return "vb_noise_filter"

    def requires_scheduled_sell(self) -> bool:
        return True

    def should_buy(self, ticker: str, current_price: float) -> BuySignal:
        try:
            k_raw            = self._md.compute_noise_filter_k(ticker, days=config.NOISE_FILTER_DAYS)
            k                = max(_K_MIN, min(_K_MAX, k_raw))   # 클램프 강화
            target_price     = self._md.compute_target_price(ticker, k)
            ma               = self._md.compute_ma(ticker, period=config.MA_PERIOD)
            vol_cur, vol_sma = self._md.compute_volume_sma_intraday(ticker, _VOL_SMA_PERIOD, "minute5")
            # [개선] 당일 고가 이격도 필터용: 일봉 OHLCV (최근 3봉, 마지막이 당일)
            df_daily         = self._md.get_ohlcv(ticker, count=3)
        except DataFetchError as e:
            logger.warning(f"[vb_noise_filter] 데이터 조회 실패, 매수 건너뜀 | {ticker}: {e}")
            return BuySignal(
                ticker=ticker,
                should_buy=False,
                current_price=current_price,
                reason="DATA_ERROR",
            )

        breakout  = current_price >= target_price
        uptrend   = current_price > ma
        vol_ratio = vol_cur / vol_sma if vol_sma > 0 else 0.0
        vol_ok    = vol_ratio >= _VOL_MULT

        if breakout and uptrend and vol_ok:
            reason = "BREAKOUT+MA_FILTER+VOL"
            should = True
        elif breakout and uptrend and not vol_ok:
            reason = f"BREAKOUT+MA_NO_VOL({vol_ratio:.2f}x<{_VOL_MULT}x)"
            should = False
        elif breakout and not uptrend:
            reason = "BREAKOUT_NO_UPTREND"
            should = False
        else:
            reason = "NO_BREAKOUT"
            should = False

        # ── [개선] 고점 이격도 필터: 당일 고가 아래 1% 이내에서의 돌파는 패스 ──
        # 당일 고가 바로 아래에서 돌파 진입 시 윗꼬리 저항 맞을 확률이 높음.
        # 단, 이미 당일 고가를 갱신(신고가) 중인 경우는 적용하지 않음 (진짜 돌파).
        if should and not df_daily.empty:
            try:
                today_high = float(df_daily.iloc[-1]["high"])
                # 현재가가 오늘 고가를 갱신하지 못했을 때만 필터 적용
                if today_high > 0 and current_price < today_high:
                    proximity_pct = (today_high - current_price) / today_high * 100
                    if proximity_pct < 1.0:
                        # 고가 아래 1% 이내 → 윗꼬리 저항 위험 → 매수 패스
                        should = False
                        reason = (
                            f"HIGH_PROXIMITY({proximity_pct:.2f}%<1%"
                            f"|high={today_high:,.0f})"
                        )
            except Exception as e:
                logger.debug(f"[vb_noise_filter] 고점 이격도 계산 오류: {e}")

        logger.debug(
            f"[vb_noise_filter] {ticker} | price={current_price:,.0f} "
            f"target={target_price:,.0f} ma={ma:,.0f} "
            f"k={k:.3f}(raw={k_raw:.3f}) vol={vol_ratio:.2f}x → {reason}"
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
                "ma_15": round(ma, 0),
                "vol_ratio": round(vol_ratio, 2),
            },
        )

    def should_sell_on_signal(
        self, ticker: str, current_price: float, position
    ) -> SellSignal:
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
                logger.info(f"[vb_noise_filter] ★ 트레일링 청산 | {ticker} | pnl={pnl_pct:+.2f}% | {reason}")
                self._peaks.pop(ticker, None)
                self._time_cut_extended.discard(ticker)  # [개선] 포지션 종료 시 연장 기록 초기화
                return SellSignal(ticker, True, current_price, reason)

        # ── 본절 방어: peak PnL ≥ BE_TRIGGER이면 SL을 진입가+BE_FLOOR로 ──
        if peak_pnl_pct >= _BE_TRIGGER_PCT:
            be_floor_price = entry * (1 + _BE_FLOOR_PCT / 100)
            if current_price <= be_floor_price:
                reason = (
                    f"BREAKEVEN_DEFENSE(peak={peak_pnl_pct:+.2f}%"
                    f"|floor={_BE_FLOOR_PCT}%|pnl={pnl_pct:+.2f}%)"
                )
                logger.info(f"[vb_noise_filter] ★ 본절방어 청산 | {ticker} | {reason}")
                self._peaks.pop(ticker, None)
                self._time_cut_extended.discard(ticker)  # [개선] 포지션 종료 시 연장 기록 초기화
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
                    # ── [개선] 모멘텀 유지 시 타임컷 1회 연장 ──────────────────
                    # 수익률이 마이너스여도 1시간봉 MACD 양봉 + RSI≥50 이면
                    # 추세가 살아있다고 판단 → 타임컷을 딱 1회 바이패스(연장)
                    # 이미 1회 연장 사용한 경우에는 무조건 청산 (무한 연장 방지)
                    if ticker not in self._time_cut_extended:
                        # 아직 연장 미사용 → 모멘텀 지표 확인
                        try:
                            macd_1h = self._md.compute_macd(ticker, 12, 26, 9, "minute60")
                            rsi_1h  = self._md.compute_rsi_intraday(ticker, 14, "minute60")
                            # 1시간봉 기준 MACD 히스토그램 양수 AND RSI ≥ 50
                            momentum_alive = macd_1h["hist"] > 0 and rsi_1h >= 50
                        except Exception as e:
                            logger.debug(f"[vb_noise_filter] 모멘텀 지표 조회 오류: {e}")
                            momentum_alive = False

                        if momentum_alive:
                            # 모멘텀 유지 중 → 타임컷 1회 연장, 보유 유지
                            self._time_cut_extended.add(ticker)
                            logger.info(
                                f"[vb_noise_filter] ⏱ 타임컷 연장 | {ticker} | "
                                f"pnl={pnl_pct:+.2f}% elapsed={elapsed_hours:.1f}h | "
                                f"MACD_1H={macd_1h['hist']:.4f} RSI_1H={rsi_1h:.1f} "
                                f"→ 모멘텀 유지 중, 1회 바이패스 허용"
                            )
                            # 이번 사이클 청산하지 않고 통과
                        else:
                            # 모멘텀 소진 → 즉시 타임컷 청산
                            reason = (
                                f"TIME_CUT({elapsed_hours:.1f}h"
                                f"|pnl={pnl_pct:+.2f}%<{_MIN_MOMENTUM_PCT}%"
                                f"|모멘텀소진)"
                            )
                            logger.info(
                                f"[vb_noise_filter] ⏱ 타임컷 청산 | {ticker} | {reason}"
                            )
                            self._peaks.pop(ticker, None)
                            self._time_cut_extended.discard(ticker)
                            return SellSignal(ticker, True, current_price, reason)
                    else:
                        # 이미 1회 연장 사용함 → 무조건 최종 타임컷 청산 (무한 연장 방지)
                        reason = (
                            f"TIME_CUT_FINAL({elapsed_hours:.1f}h"
                            f"|pnl={pnl_pct:+.2f}%<{_MIN_MOMENTUM_PCT}%"
                            f"|연장소진)"
                        )
                        logger.info(
                            f"[vb_noise_filter] ⏱ 타임컷 최종청산(연장 소진) | {ticker} | {reason}"
                        )
                        self._peaks.pop(ticker, None)
                        self._time_cut_extended.discard(ticker)
                        return SellSignal(ticker, True, current_price, reason)
        except Exception as e:
            logger.debug(f"[vb_noise_filter] time-cut 계산 오류: {e}")

        # VB 전략은 기본적으로 스케줄(09:00) 또는 손절로만 매도
        return SellSignal(ticker, False, current_price, "")
