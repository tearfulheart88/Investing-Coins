"""
전략: SMRH 스탑매매 (멀티 타임프레임 하이킨아시 돌파)
시나리오 ID: smrh_stop

■ 개요
  4시간봉(상위 추세 확인) + 30분봉(진입 트리거) 이중 필터.
  상위 추세가 모두 정렬된 상태에서 30분봉 하이킨아시 첫 양봉전환 고점을
  현재가가 돌파할 때 스탑매수 진입.

■ 매수 조건

  [4h 상위 추세 필터 — 전부 AND]
  1. MACD(12,26,70) 히스토그램 > 0          (4h 상승 추세)
  2. Stochastic(K=12,D=3) %K > %D           (4h 스토케스틱 양성)
  3. RSI(14) ≥ RSI_MIN(50)                  (4h 모멘텀 양성)
  4. 현재가 ≥ 20일 이동평균(일봉)            (일봉 추세 양성)
  5. 4h 하이킨아시 강한 양봉                 (4h HA 양봉 + 아래꼬리 없음: |HA_open - HA_low| / HA_open < 0.02%)

  [30m 진입 트리거 — RSI·HA AND, MACD/Stoch OR]
  6. RSI(14) ≥ RSI_MIN(50) (30m)
  7. 30m 하이킨아시: 양봉 또는 양봉전환
  8. MACD(12,26,70) 히스트 음→양 돌파  OR  Stochastic %K 상향 크로스  (둘 중 하나)
  9. 현재가 > 30m 하이킨아시 첫 양봉전환 봉 고점  ← 스탑 매수 트리거

■ 매도 조건
  SL1 : 30m MACD 히스토그램 음수 전환  (단기 추세 반전)
  SL2 : 4h 하이킨아시 음봉전환          (상위 추세 이탈)
  SL3 : 30m 진입 캔들 저가 이탈         (v3: 빠른 하드 스톱 — 4h 대기 없이 즉시 청산)
  (하드 손절은 risk_manager 처리)

■ 타임프레임
  4h  = minute240
  30m = minute30

■ 참고
  MACD 시그널 기간 70은 원본 전략 명세 그대로 사용
  (표준 9보다 길어 추세 필터 역할 강화).
  Stochastic 파라미터: K=12, D=3, Smooth=3
"""
import logging

from data.market_data import MarketData
from exchange.upbit_client import DataFetchError
from strategies.base_strategy import BaseStrategy, BuySignal, SellSignal

logger = logging.getLogger(__name__)

_INTERVAL_4H  = "minute240"
_INTERVAL_30M = "minute30"

# ── MACD 파라미터 (원본 명세 그대로) ────────────────────────────────────────
_MACD_FAST   = 12
_MACD_SLOW   = 26
_MACD_SIGNAL = 70    # 원본 전략 명세값 (표준 9보다 길어 노이즈 감소)

# ── Stochastic 파라미터 ─────────────────────────────────────────────────────
_STOCH_K      = 12
_STOCH_D      = 3
_STOCH_SMOOTH = 3

# ── 공통 파라미터 ───────────────────────────────────────────────────────────
_RSI_PERIOD     = 14
_RSI_MIN        = 50.0   # 4h + 30m 공통 RSI 최소 기준
_MA_PERIOD      = 20     # 일봉 이동평균 기간

# ── HA 하단꼬리 허용 비율 ────────────────────────────────────────────────────
# 4h HA 캔들의 하단꼬리가 HA_open 대비 이 비율 이하여야 "강한 양봉"으로 인정
# 0.0002(0.02%): 원래값 — 사실상 꼬리 없는 봉만 허용 (극히 드묾 → 0거래)
# 0.005 (0.5%): 소폭 꼬리 허용 — 현실적인 기준
_HA_WICK_PCT    = 0.005  # v2: 0.0002 → 0.005 (4h HA 진입 기회 확대)

# ── [개선] 거래량 필터 파라미터 ─────────────────────────────────────────────
# 가짜 돌파(Fakeout) 방지: 돌파 시점의 30m 거래량이 충분해야만 진짜 돌파로 인정
_VOL_MULT_30M   = 1.5    # 진짜 돌파 인정 거래량 배수 (직전 N봉 평균 대비)
_VOL_SMA_PERIOD = 20     # 거래량 SMA 산출 기간 (30m봉 기준)


class SMRHStopStrategy(BaseStrategy):
    """SMRH 스탑매매: 멀티 타임프레임 하이킨아시 돌파 전략."""

    def __init__(self, market_data: MarketData) -> None:
        self._md = market_data
        self._entry_lows: dict[str, float] = {}  # v3: ticker → 진입 시 30m 캔들 저가 (하드 스톱)

    def get_strategy_id(self) -> str:
        return "trend_following"

    def get_scenario_id(self) -> str:
        return "smrh_stop"

    def requires_scheduled_sell(self) -> bool:
        return False   # 신호 기반 청산 (TP/SL) → 동일 종목 재진입 가능

    # ─── 매수 신호 ────────────────────────────────────────────────────────────

    def should_buy(self, ticker: str, current_price: float) -> BuySignal:
        try:
            # 4h 지표
            macd_4h  = self._md.compute_macd(ticker, _MACD_FAST, _MACD_SLOW, _MACD_SIGNAL, _INTERVAL_4H)
            stoch_4h = self._md.compute_stochastic(ticker, _STOCH_K, _STOCH_D, _STOCH_SMOOTH, _INTERVAL_4H)
            rsi_4h   = self._md.compute_rsi_intraday(ticker, _RSI_PERIOD, _INTERVAL_4H)
            ma_20d   = self._md.compute_ma(ticker, _MA_PERIOD)
            ha_4h    = self._md.compute_ha_intraday(ticker, _INTERVAL_4H, count=10)
            # 30m 지표
            macd_30m  = self._md.compute_macd(ticker, _MACD_FAST, _MACD_SLOW, _MACD_SIGNAL, _INTERVAL_30M)
            stoch_30m = self._md.compute_stochastic(ticker, _STOCH_K, _STOCH_D, _STOCH_SMOOTH, _INTERVAL_30M)
            rsi_30m   = self._md.compute_rsi_intraday(ticker, _RSI_PERIOD, _INTERVAL_30M)
            ha_30m    = self._md.compute_ha_intraday(ticker, _INTERVAL_30M, count=50)
            # [개선] 30m 거래량 — 가짜 돌파(Fakeout) 필터용: 진입 시점 거래량 확인
            vol_cur_30m, vol_sma_30m = self._md.compute_volume_sma_intraday(
                ticker, _VOL_SMA_PERIOD, _INTERVAL_30M
            )
        except DataFetchError as e:
            logger.warning(f"[smrh] 데이터 오류: {ticker} - {e}")
            return BuySignal(ticker, False, current_price, "DATA_ERROR")

        ha_4h_last    = ha_4h.iloc[-1]
        ha_4h_bullish = bool(ha_4h_last["is_bullish"])

        # 아래꼬리 없는 강한 양봉 체크: |HA_open - HA_low| / HA_open < 0.0002 (0.02%)
        ha_open_4h = float(ha_4h_last["open"])
        ha_low_4h  = float(ha_4h_last["low"])
        ha_4h_no_lower_wick = (
            ha_4h_bullish and
            (abs(ha_open_4h - ha_low_4h) / ha_open_4h < _HA_WICK_PCT if ha_open_4h > 0 else False)
        )

        meta = {
            "macd_4h_hist":      round(macd_4h["hist"],  6),
            "stoch_4h_k":        round(stoch_4h["k"],    1),
            "stoch_4h_d":        round(stoch_4h["d"],    1),
            "rsi_4h":            round(rsi_4h,            1),
            "ma_20d":            round(ma_20d,            0),
            "ha_4h":             "강한양봉" if ha_4h_no_lower_wick else ("양봉" if ha_4h_bullish else "음봉"),
            "ha_4h_open":        round(ha_open_4h,        0),
            "ha_4h_low":         round(ha_low_4h,         0),
            "macd_30m_hist":     round(macd_30m["hist"],  6),
            "stoch_30m_k":       round(stoch_30m["k"],    1),
            "stoch_30m_d":       round(stoch_30m["d"],    1),
            "rsi_30m":           round(rsi_30m,            1),
            # [개선] 거래량 비율: 진짜 돌파 확인용
            "vol_ratio_30m":     round(vol_cur_30m / vol_sma_30m if vol_sma_30m > 0 else 0.0, 2),
        }

        # ── 4h 상위 추세 필터 (전부 AND) ─────────────────────────────────────
        cond_4h_macd  = macd_4h["hist"] > 0
        cond_4h_stoch = stoch_4h["k"] > stoch_4h["d"]
        cond_4h_rsi   = rsi_4h >= _RSI_MIN
        cond_4h_ma    = current_price >= ma_20d
        # HA 필터: 양봉 + 아래꼬리 없는 강한 양봉 (낙칼 잡기 억제)
        cond_4h_ha    = ha_4h_no_lower_wick

        if not all([cond_4h_macd, cond_4h_stoch, cond_4h_rsi, cond_4h_ma, cond_4h_ha]):
            failed = []
            if not cond_4h_macd:  failed.append(f"MACD_4H_NEG({macd_4h['hist']:.4f})")
            if not cond_4h_stoch: failed.append(f"STOCH_4H_K<D({stoch_4h['k']:.1f}<{stoch_4h['d']:.1f})")
            if not cond_4h_rsi:   failed.append(f"RSI_4H_LOW({rsi_4h:.1f}<{_RSI_MIN})")
            if not cond_4h_ma:    failed.append(f"BELOW_MA20({current_price:,.0f}<{ma_20d:,.0f})")
            if not cond_4h_ha:
                if not ha_4h_bullish:
                    failed.append("HA_4H_BEAR")
                else:
                    wick_ratio = abs(ha_open_4h - ha_low_4h) / ha_open_4h * 100 if ha_open_4h > 0 else 0
                    failed.append(f"HA_4H_HAS_LOWER_WICK({wick_ratio:.3f}%>=0.02%)")
            logger.debug(f"[smrh] {ticker} | 4H_FAIL:" + ",".join(failed))
            return BuySignal(ticker, False, current_price,
                             "4H_FILTER:" + ",".join(failed), metadata=meta)

        # ── 30m 진입 조건 ─────────────────────────────────────────────────────
        ha_30m_last = ha_30m.iloc[-1]
        cond_30m_rsi = rsi_30m >= _RSI_MIN
        cond_30m_ha  = bool(ha_30m_last["is_bullish"]) or bool(ha_30m_last["turned_bullish"])

        # 돌파: MACD 히스트 음→양  OR  Stochastic %K 상향 크로스
        macd_cross  = macd_30m["hist_prev"] < 0 and macd_30m["hist"] > 0
        stoch_cross = stoch_30m["k_prev"] <= stoch_30m["d_prev"] and stoch_30m["k"] > stoch_30m["d"]
        cond_30m_cross = macd_cross or stoch_cross

        if not (cond_30m_rsi and cond_30m_ha and cond_30m_cross):
            failed = []
            if not cond_30m_rsi:   failed.append(f"RSI_30M_LOW({rsi_30m:.1f})")
            if not cond_30m_ha:    failed.append("HA_30M_BEAR")
            if not cond_30m_cross: failed.append(
                f"NO_CROSS(MACD:{macd_30m['hist_prev']:.4f}→{macd_30m['hist']:.4f}"
                f"|Stoch_K:{stoch_30m['k_prev']:.1f}→{stoch_30m['k']:.1f})"
            )
            logger.debug(f"[smrh] {ticker} | 30M_FAIL:" + ",".join(failed))
            return BuySignal(ticker, False, current_price,
                             "30M_FILTER:" + ",".join(failed), metadata=meta)

        # ── [개선] 거래량 필터: 돌파 시점 30m 거래량 ≥ 직전 20봉 평균 × 1.5 ──
        # 가짜 돌파(Fakeout) 방지: 거래량이 뒷받침되는 진짜 돌파만 진입
        # 거래량이 평균 이하인 돌파는 세력 없는 가짜 돌파일 가능성이 높음
        vol_ratio_30m = vol_cur_30m / vol_sma_30m if vol_sma_30m > 0 else 0.0
        if vol_ratio_30m < _VOL_MULT_30M:
            logger.debug(
                f"[smrh] {ticker} | VOL_WEAK_30M: "
                f"{vol_ratio_30m:.2f}x < {_VOL_MULT_30M}x (가짜돌파 의심 → 패스)"
            )
            return BuySignal(
                ticker, False, current_price,
                f"VOL_WEAK_30M({vol_ratio_30m:.2f}x<{_VOL_MULT_30M}x)",
                metadata=meta,
            )

        # ── 스탑 매수 트리거: 하이킨아시 첫 양봉전환 봉 고점 돌파 ─────────────
        turned_rows = ha_30m[ha_30m["turned_bullish"]]
        if turned_rows.empty:
            logger.debug(f"[smrh] {ticker} | NO_HA_TURN (양봉전환 봉 없음)")
            return BuySignal(ticker, False, current_price,
                             "NO_HA_TURN_BULLISH", metadata=meta)

        breakout_target = float(turned_rows.iloc[-1]["high"])
        meta["ha_breakout_target"] = round(breakout_target, 0)

        if current_price <= breakout_target:
            logger.debug(
                f"[smrh] {ticker} | BELOW_TARGET "
                f"({current_price:,.0f} <= {breakout_target:,.0f})"
            )
            return BuySignal(
                ticker, False, current_price,
                f"BELOW_HA_TARGET({current_price:,.0f}<={breakout_target:,.0f})",
                metadata=meta,
            )

        # ── 전체 조건 충족 → 매수 ────────────────────────────────────────────
        cross_str = "+".join(filter(None, [
            "MACD크로스" if macd_cross  else "",
            "Stoch크로스" if stoch_cross else "",
        ]))

        # v3: 30m 진입 캔들 저가 저장 (SL3 하드 스톱용)
        entry_30m_low = float(ha_30m.iloc[-1]["low"])
        self._entry_lows[ticker] = entry_30m_low
        meta["entry_30m_low"] = round(entry_30m_low, 0)

        logger.info(
            f"[smrh] ★ 매수신호 | {ticker} | "
            f"현재가={current_price:,.0f} > HA목표={breakout_target:,.0f} | "
            f"4h[MACD✓ Stoch✓ RSI={rsi_4h:.1f} MA20✓ HA강한양봉✓] | "
            f"30m[{cross_str} RSI={rsi_30m:.1f}] | "
            f"SL3_low={entry_30m_low:,.0f}"
        )
        return BuySignal(
            ticker=ticker,
            should_buy=True,
            current_price=current_price,
            reason=f"SMRH_STOP({cross_str})",
            metadata=meta,
        )

    # ─── 매도 신호 ────────────────────────────────────────────────────────────

    def should_sell_on_signal(
        self, ticker: str, current_price: float, position
    ) -> SellSignal:
        # ── SL3: 30m 진입 캔들 저가 이탈 (빠른 하드 스톱) ──────────────────
        # v3: 4h HA 반전까지 기다리지 않고 30m 수준에서 즉시 청산
        entry_low = self._entry_lows.get(ticker)
        if entry_low is not None and current_price < entry_low:
            pnl_pct = (current_price - position.buy_price) / position.buy_price * 100
            reason = f"HARD_STOP_30M_LOW({current_price:,.0f}<{entry_low:,.0f})"
            logger.info(
                f"[smrh] 매도(30m저가이탈) | {ticker} | "
                f"수익={pnl_pct:+.2f}% | {reason}"
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

        # ── SL1: 30m MACD 히스토그램 음수 전환 — 직전 완성 캔들 기준 ──────────
        # [개선] 실시간 틱(hist) → 직전 종가 캔들(hist_prev) 기준으로 변경
        # 이유: 현재 미완성 캔들이 일시적으로 음수를 보이다가 복귀하는
        #       '휩쏘(Whipsaw)' 현상에 의한 불필요한 손절매 방지
        if macd_30m["hist_prev"] < 0:
            reason = f"MACD_30M_NEG_PREV({macd_30m['hist_prev']:.4f})"
            logger.info(f"[smrh] 매도(MACD반전-직전봉) | {ticker} | {reason}")
            self._entry_lows.pop(ticker, None)
            return SellSignal(ticker, True, current_price, reason)

        # ── SL2: 4h 하이킨아시 음봉전환 (상위 추세 이탈) ─────────────────────
        ha_last = ha_4h.iloc[-1]
        ha_prev = ha_4h.iloc[-2]
        if bool(ha_prev["is_bullish"]) and not bool(ha_last["is_bullish"]):
            pnl_pct = (current_price - position.buy_price) / position.buy_price * 100
            reason  = "HA_4H_TURNED_BEAR"
            logger.info(
                f"[smrh] 매도(4H HA반전) | {ticker} | "
                f"수익={pnl_pct:+.2f}% | {reason}"
            )
            self._entry_lows.pop(ticker, None)
            return SellSignal(ticker, True, current_price, reason)

        return SellSignal(ticker, False, current_price, "")
