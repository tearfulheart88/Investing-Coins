"""
전략: 변동성 돌파 + 노이즈 필터 + 이동평균선 필터 (개선판 v4)
시나리오 ID: vb_noise_filter

■ 개선 사항 (v4) — 2026-03-07 Gemini 분석 기반
  - 파라미터 일원화: 모든 상수를 config.STRATEGY_PARAMS["vb"]에서 초기화
  - 스케줄 매도 시 _peaks/_time_cut_extended 자동 정리 (on_position_closed)
  - BE/TSL 파라미터를 config + UI 슬라이더에서 조정 가능
  - 로깅 상세화: 매도 신호마다 PnL, peak, 파라미터 값 기록

■ 개선 사항 (v3) — 2026-03-05 일보 기반
  - 타임컷 완화: 1h/1% → 2h/0.3% (수익 거래 조기 청산 방지)
  - 본절 방어: peak PnL ≥ 1.0% 도달 시 SL을 진입가+0.2%로 이동
  - 트레일링: peak에서 -0.5% 하락 시 청산

■ 개선 사항 (v2)
  1. K 클램프 강화: max(K_MIN, min(K_MAX, k))
  2. 거래량 확인 필터: 5분봉 현재 거래량 >= Vol_SMA(20) × VOL_MULT
  3. datetime 타입 가드: position.buy_time이 str 또는 datetime 모두 처리

매수 조건 (AND):
  1. current_price >= today_open + yesterday_range * k  (변동성 돌파)
  2. current_price > MA(15)                             (상승 추세 확인)
  3. vol_cur_5m >= vol_sma_5m × VOL_MULT               (거래량 급증 확인)
  4. 당일 고가 이격도 >= 1%                              (윗꼬리 저항 회피)

매도 전략:
  TP  : 익일 09:00 KST 스케줄 매도 (requires_scheduled_sell=True)
  SL  : risk_manager 손절
  TSL : peak에서 -TRAIL_DROP_PCT% 하락 → 트레일링 청산  (우선순위 1)
  BE  : peak PnL ≥ BE_TRIGGER% → 현재가 ≤ 진입가+BE_FLOOR% → 본절 청산  (우선순위 2)
  TC  : 매수 후 TIME_CUT_HOURS 경과 + 수익률 < MIN_MOMENTUM% → 청산  (우선순위 3)
       (단, 1h봉 MACD+RSI 모멘텀 유지 시 1회 연장)

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

# ─── 모듈 상수 (config.STRATEGY_PARAMS["vb"]에서 초기화, UI 슬라이더로 런타임 변경) ──
_vb = config.STRATEGY_PARAMS.get("vb", {})
_K_MIN              = _vb.get("k_min",            0.3)
_K_MAX              = _vb.get("k_max",            0.8)
_TIME_CUT_HOURS     = _vb.get("time_cut_hours",   2.0)
_MIN_MOMENTUM_PCT   = _vb.get("min_momentum_pct", 0.3)
_VOL_MULT           = _vb.get("vol_mult",         2.5)
_VOL_SMA_PERIOD     = 20
_BE_TRIGGER_PCT     = _vb.get("be_trigger_pct",   1.0)
_BE_FLOOR_PCT       = _vb.get("be_floor_pct",     0.2)
_TRAIL_DROP_PCT     = _vb.get("trail_drop_pct",   0.5)

_KST = timezone(timedelta(hours=9))


class VBNoiseFilterStrategy(BaseStrategy):

    def __init__(self, market_data: MarketData) -> None:
        self._md = market_data
        self._peaks: dict[str, float] = {}          # ticker → 최고가 (본절방어 + 트레일링)
        self._time_cut_extended: set[str] = set()    # 타임컷 1회 연장 사용 추적

    def get_strategy_id(self) -> str:
        return "volatility_breakout"

    def get_scenario_id(self) -> str:
        return "vb_noise_filter"

    def requires_scheduled_sell(self) -> bool:
        return True

    # ─── 포지션 종료 시 내부 상태 정리 (스케줄매도/외부 매도 공통) ────────────
    def on_position_closed(self, ticker: str) -> None:
        """
        포지션 종료 시 호출. 스케줄 매도(09:00), 손절, 수동 청산 등
        어떤 경로로든 포지션이 종료되면 내부 추적 상태를 정리한다.
        """
        self._peaks.pop(ticker, None)
        self._time_cut_extended.discard(ticker)

    def reset_daily(self) -> None:
        """09:00 스케줄 매도 후 일괄 초기화."""
        self._peaks.clear()
        self._time_cut_extended.clear()

    # ─── 매수 ────────────────────────────────────────────────────────────────

    def should_buy(self, ticker: str, current_price: float) -> BuySignal:
        try:
            k_raw            = self._md.compute_noise_filter_k(ticker, days=config.NOISE_FILTER_DAYS)
            k                = max(_K_MIN, min(_K_MAX, k_raw))
            target_price     = self._md.compute_target_price(ticker, k)
            ma               = self._md.compute_ma(ticker, period=config.MA_PERIOD)
            vol_cur, vol_sma = self._md.compute_volume_sma_intraday(ticker, _VOL_SMA_PERIOD, "minute5")
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

        # ── 고점 이격도 필터: 당일 고가 아래 1% 이내 → 윗꼬리 저항 회피 ────
        if should and not df_daily.empty:
            try:
                today_high = float(df_daily.iloc[-1]["high"])
                if today_high > 0 and current_price < today_high:
                    proximity_pct = (today_high - current_price) / today_high * 100
                    if proximity_pct < 1.0:
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

    # ─── 매도 ────────────────────────────────────────────────────────────────

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

        # ── [1순위] 트레일링: peak에서 TRAIL_DROP_PCT 이상 하락 시 청산 ──────
        if peak_pnl_pct >= _BE_TRIGGER_PCT:
            drop_from_peak = (peak - current_price) / peak * 100
            if drop_from_peak >= _TRAIL_DROP_PCT:
                reason = (
                    f"TRAIL_DROP(peak={peak_pnl_pct:+.2f}%"
                    f"|drop={drop_from_peak:.2f}%>={_TRAIL_DROP_PCT}%"
                    f"|pnl={pnl_pct:+.2f}%)"
                )
                logger.info(
                    f"[vb_noise_filter] ★ 트레일링 청산 | {ticker} | "
                    f"entry={entry:,.0f} peak={peak:,.0f} now={current_price:,.0f} | {reason}"
                )
                self.on_position_closed(ticker)
                return SellSignal(ticker, True, current_price, reason)

        # ── [2순위] 본절 방어: peak ≥ trigger → 현재가 ≤ floor가 → 청산 ────
        if peak_pnl_pct >= _BE_TRIGGER_PCT:
            be_floor_price = entry * (1 + _BE_FLOOR_PCT / 100)
            if current_price <= be_floor_price:
                reason = (
                    f"BREAKEVEN_DEFENSE(peak={peak_pnl_pct:+.2f}%"
                    f"|floor={_BE_FLOOR_PCT}%|pnl={pnl_pct:+.2f}%"
                    f"|floor_price={be_floor_price:,.0f})"
                )
                logger.info(
                    f"[vb_noise_filter] ★ 본절방어 청산 | {ticker} | "
                    f"entry={entry:,.0f} peak={peak:,.0f} now={current_price:,.0f} | {reason}"
                )
                self.on_position_closed(ticker)
                return SellSignal(ticker, True, current_price, reason)

        # ── [3순위] Time-cut: 일정 시간 경과 + 모멘텀 부재 → 청산 ───────────
        try:
            buy_time = position.buy_time
            if isinstance(buy_time, str):
                buy_time = datetime.fromisoformat(buy_time)
            if buy_time.tzinfo is None:
                buy_time = buy_time.replace(tzinfo=_KST)
            elapsed_hours = (datetime.now(_KST) - buy_time).total_seconds() / 3600

            if elapsed_hours >= _TIME_CUT_HOURS and pnl_pct < _MIN_MOMENTUM_PCT:
                # 아직 연장 미사용 → 모멘텀 지표 확인 후 1회 연장 가능
                if ticker not in self._time_cut_extended:
                    momentum_alive = False
                    macd_hist_val, rsi_val = 0.0, 0.0
                    try:
                        macd_1h = self._md.compute_macd(ticker, 12, 26, 9, "minute60")
                        rsi_1h  = self._md.compute_rsi_intraday(ticker, 14, "minute60")
                        macd_hist_val = macd_1h["hist"]
                        rsi_val = rsi_1h
                        momentum_alive = macd_hist_val > 0 and rsi_val >= 50
                    except Exception as e:
                        logger.debug(f"[vb_noise_filter] 모멘텀 지표 조회 오류: {e}")

                    if momentum_alive:
                        self._time_cut_extended.add(ticker)
                        logger.info(
                            f"[vb_noise_filter] ⏱ 타임컷 연장 | {ticker} | "
                            f"pnl={pnl_pct:+.2f}% elapsed={elapsed_hours:.1f}h | "
                            f"MACD_1H={macd_hist_val:.4f} RSI_1H={rsi_val:.1f} "
                            f"→ 모멘텀 유지, 1회 바이패스"
                        )
                        # 연장 — 이번 사이클 보유 유지
                    else:
                        reason = (
                            f"TIME_CUT({elapsed_hours:.1f}h"
                            f"|pnl={pnl_pct:+.2f}%<{_MIN_MOMENTUM_PCT}%"
                            f"|MACD={macd_hist_val:.4f}|RSI={rsi_val:.1f})"
                        )
                        logger.info(
                            f"[vb_noise_filter] ⏱ 타임컷 청산 | {ticker} | "
                            f"entry={entry:,.0f} now={current_price:,.0f} | {reason}"
                        )
                        self.on_position_closed(ticker)
                        return SellSignal(ticker, True, current_price, reason)
                else:
                    # 이미 1회 연장 사용 → 무조건 최종 청산 (무한 연장 방지)
                    reason = (
                        f"TIME_CUT_FINAL({elapsed_hours:.1f}h"
                        f"|pnl={pnl_pct:+.2f}%<{_MIN_MOMENTUM_PCT}%"
                        f"|연장소진)"
                    )
                    logger.info(
                        f"[vb_noise_filter] ⏱ 타임컷 최종청산 | {ticker} | "
                        f"entry={entry:,.0f} now={current_price:,.0f} | {reason}"
                    )
                    self.on_position_closed(ticker)
                    return SellSignal(ticker, True, current_price, reason)
        except Exception as e:
            logger.debug(f"[vb_noise_filter] time-cut 계산 오류: {e}")

        # VB 전략은 기본적으로 스케줄(09:00) 또는 손절로만 매도
        return SellSignal(ticker, False, current_price, "")
