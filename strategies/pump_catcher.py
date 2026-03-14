"""
전략: 비정상 거래량 폭발 + 단기 급등 스캘핑 (Volume Anomaly Pump Catcher)
시나리오 ID: pump_catcher

■ 전략 개요
  - 1분봉 기준 거래량 폭발(SMA×15↑) + 장대양봉(+3%↑)을 동시에 감지하여
    알트코인 초기 펌핑에 빠르게 진입 → 수익 확정 후 즉시 이탈하는 스캘핑 전략
  - "설거지" 방지를 위한 7중 진입 필터 + 4중 청산 로직

■ 매수 조건 (AND 7개)
  1. 거래량 폭발:   1m 거래량 >= SMA20(1m) × vol_mult (기본 15배)
  2. 장대양봉:      (현재가-시가)/시가 >= spike_pct% (기본 +3%)
  3. 고점 추격 방지: 현재가 <= 일봉 시가 × (1 + max_gain_from_open%) (기본 +15%)
  4. 양봉 품질:     (현재가-시가)/(고가-저가) >= min_body_ratio (기본 0.5) — 설거지 위꼬리 방지
  5. RSI 과열 방지: RSI(7, 1m) <= rsi_max (기본 85) — 고점 진입 차단
  6. 하락 전환 방지: 현재가 >= 당 1m봉 고가 × 0.95 — 이미 떨어지는 중이면 패스
  7. 쿨다운:        동일 종목 최근 매도 후 cooldown_minutes(기본 30분) 경과 필요

■ 매도 조건 (우선순위순)
  1. 하드 손절:        진입가 대비 -hard_sl_pct% (기본 -3.0%) → 즉시 청산 (설거지 물림 방지)
  2. 트레일링 스탑:    peak에서 effective_trail% 하락 → 즉시 청산
       - 기본:              trail_pct% (2.0%)
       - peak >= tp_lock%:  trail_locked_pct% (1.0%) 로 자동 축소 (수익 극대화)
  3. 거래량 소멸:      1m 거래량 < SMA20 × vol_fade_mult AND PnL < 1% → 모멘텀 소멸 → 청산
  4. 타임컷:           max_hold_minutes(기본 10분) 초과 AND PnL < 1.0% → 청산

■ 주요 파라미터 (v3 기준)
  vol_mult            = 12.0   거래량 폭발 배수 (SMA20 × N배) — v3: 8→12
  spike_pct           = 2.0    양봉 최소 급등률 (%) — v3: 1.5→2.0
  max_gain_from_open  = 15.0   일봉 시가 대비 최대 허용 상승률 (%)
  min_body_ratio      = 0.5    양봉 몸통 비율 하한 (0~1)
  rsi_max             = 78.0   RSI 최대 허용값 — v3: 85→78 (고점 진입 강력 차단)
  trail_pct           = 2.0    기본 트레일링 스탑 (%)
  hard_sl_pct         = 4.5    하드 손절 (%) — v3: 3→4.5 (슬리피지 버퍼)
  tp_lock_pct         = 2.5    수익 보존 강화 발동 기준 (%) — v3: 5→2.5 (달성 가능)
  trail_locked_pct    = 1.0    수익 보존 후 좁혀진 트레일링 (%)
  vol_fade_mult       = 2.0    거래량 소멸 판정 기준 배수 (SMA × N배 미만)
  max_hold_minutes    = 15.0   최대 보유 시간 (분) — v3: 10→15
  cooldown_minutes    = 30.0   동일 종목 재진입 쿨다운 (분)

임포트 규칙:
  이 파일은 base_strategy, data.market_data 만 임포트. 다른 전략 임포트 금지.
"""
import logging
import time
from datetime import datetime, timezone, timedelta

from data.market_data import MarketData
from exchange.upbit_client import DataFetchError
from strategies.base_strategy import BaseStrategy, BuySignal, SellSignal
import config

logger = logging.getLogger(__name__)

# ─── 모듈 상수 (config.STRATEGY_PARAMS["pump_catcher"]에서 초기화) ─────────
_pc = config.STRATEGY_PARAMS.get("pump_catcher", {})
_VOL_MULT           = _pc.get("vol_mult",             15.0)
_SPIKE_PCT          = _pc.get("spike_pct",              3.0)
_MAX_GAIN_OPEN_PCT  = _pc.get("max_gain_from_open",    15.0)
_MIN_BODY_RATIO     = _pc.get("min_body_ratio",          0.5)
_RSI_MAX            = _pc.get("rsi_max",                85.0)
_TRAIL_PCT          = _pc.get("trail_pct",               2.0)
_HARD_SL_PCT        = _pc.get("hard_sl_pct",             3.0)
_TP_LOCK_PCT        = _pc.get("tp_lock_pct",             5.0)
_TRAIL_LOCKED_PCT   = _pc.get("trail_locked_pct",        1.0)
_VOL_FADE_MULT      = _pc.get("vol_fade_mult",           2.0)
_MAX_HOLD_MIN       = _pc.get("max_hold_minutes",        10.0)
_COOLDOWN_MIN       = _pc.get("cooldown_minutes",        30.0)

_VOL_SMA_PERIOD = 20   # 거래량 SMA 기간 (고정, 1분봉 기준 20분 평균)
_KST = timezone(timedelta(hours=9))


def _rsi_last(closes: list, period: int = 7) -> float | None:
    """closes 리스트 마지막 지점의 Wilder RSI(period)를 계산.

    데이터가 부족하거나 손실 평균이 0이면 None 반환.
    market_data 없이 순수 계산 (buy 로직 내 RSI 기울기 체크용).
    """
    if len(closes) < period + 2:
        return None
    gains: list[float] = []
    losses: list[float] = []
    for i in range(1, len(closes)):
        diff = closes[i] - closes[i - 1]
        gains.append(max(diff, 0.0))
        losses.append(max(-diff, 0.0))
    # Wilder 초기 평균
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    # Wilder 평활
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    if avg_loss == 0:
        return 100.0
    return 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)


class PumpCatcherStrategy(BaseStrategy):
    """
    비정상 거래량 폭발 + 단기 급등 스캘핑 전략.
    1분봉 실시간 감지 → 빠른 진입/이탈.
    """

    def __init__(self, market_data: MarketData) -> None:
        self._md = market_data

        # peak 추적: ticker → 최고가 (트레일링 스탑용)
        self._peaks: dict[str, float] = {}

        # 수익 보존 락 활성화 여부: ticker → bool
        self._tp_locked: set[str] = set()

        # 쿨다운 추적: ticker → 마지막 매도 timestamp
        self._cooldown_map: dict[str, float] = {}

        # 매수 필터 통계
        self._buy_stats: dict[str, int] = {
            "total":         0,
            "cooldown":      0,   # 쿨다운 중
            "no_vol_spike":  0,   # 거래량 폭발 미달
            "no_spike":      0,   # 가격 급등 미달
            "overextended":  0,   # 일봉 시가 대비 너무 많이 오름
            "weak_body":     0,   # 양봉 몸통 약함 (설거지 위꼬리 의심)
            "rsi_hot":       0,   # RSI 과열 (고점 진입 방지)
            "price_fading":  0,   # 이미 하락 전환 중
            "data_error":    0,   # 데이터 조회 오류
            "passed":        0,   # 모든 필터 통과 → 매수 신호 발생
        }

    # ─── 전략 ID ─────────────────────────────────────────────────────────────

    def get_strategy_id(self) -> str:
        return "scalping"

    def get_scenario_id(self) -> str:
        return "pump_catcher"

    def requires_scheduled_sell(self) -> bool:
        # 09:00 스케줄 매도 없음 — 자체 청산 로직(TSL/SL/타임컷)만 사용
        return False

    def get_history_requirements(self) -> dict[str, int]:
        return {
            "day": 2,
            "minute1": max(_VOL_SMA_PERIOD + 1, 28),
        }

    def get_ticker_selection_profile(self) -> dict:
        return {
            "pattern": "pump_event",
            "pool_size": 180,
            "refresh_hours": 0.1667,
        }

    # ─── 포지션 종료 콜백 ─────────────────────────────────────────────────────

    def on_position_closed(self, ticker: str, reason: str = "") -> None:
        """포지션 종료 시 내부 상태 정리 + 쿨다운 시작."""
        self._peaks.pop(ticker, None)
        self._tp_locked.discard(ticker)
        # 쿨다운 시작: 같은 종목에 cooldown_minutes 동안 재진입 차단
        self._cooldown_map[ticker] = time.time()

    def reset_daily(self) -> None:
        """09:00 이후 일괄 초기화 (이 전략은 스케줄 매도 없으나 호환성 유지)."""
        if self._buy_stats["total"] > 0:
            logger.info(
                f"[pump_catcher] 일별 매수필터 통계 | "
                f"총={self._buy_stats['total']} "
                f"쿨다운={self._buy_stats['cooldown']} "
                f"거래량X={self._buy_stats['no_vol_spike']} "
                f"급등X={self._buy_stats['no_spike']} "
                f"고점추격={self._buy_stats['overextended']} "
                f"몸통약={self._buy_stats['weak_body']} "
                f"RSI과열={self._buy_stats['rsi_hot']} "
                f"하락중={self._buy_stats['price_fading']} "
                f"에러={self._buy_stats['data_error']} "
                f"통과={self._buy_stats['passed']}"
            )
        self._peaks.clear()
        self._tp_locked.clear()
        # 쿨다운은 초기화하지 않음 (일 경계 넘어도 유효하게 유지)
        for k in self._buy_stats:
            self._buy_stats[k] = 0

    # ─── 매수 ────────────────────────────────────────────────────────────────

    def should_buy(self, ticker: str, current_price: float) -> BuySignal:
        self._buy_stats["total"] += 1

        # ── [0] 쿨다운 체크 ──────────────────────────────────────────────────
        # 한 번 설거지당하거나 타임컷된 종목은 N분간 재진입을 막아
        # 연속 손실을 방지한다
        last_sell_ts = self._cooldown_map.get(ticker)
        if last_sell_ts and (time.time() - last_sell_ts) < _COOLDOWN_MIN * 60:
            self._buy_stats["cooldown"] += 1
            elapsed_min = (time.time() - last_sell_ts) / 60
            return BuySignal(
                ticker=ticker, should_buy=False, current_price=current_price,
                reason=f"COOLDOWN({elapsed_min:.1f}min<{_COOLDOWN_MIN:.0f}min)",
            )

        # ── 데이터 수집 ──────────────────────────────────────────────────────
        try:
            # 1분봉 OHLCV: SMA20 계산을 위해 현재봉 포함 25개 (여유 5개)
            df_1m  = self._md.get_ohlcv_intraday(ticker, interval="minute1", count=25)
            # 일봉: 오늘의 시초가(open) 확인용
            df_day = self._md.get_ohlcv(ticker, count=2)
        except DataFetchError as e:
            self._buy_stats["data_error"] += 1
            logger.warning(f"[pump_catcher] 데이터 조회 실패: {ticker}: {e}")
            return BuySignal(
                ticker=ticker, should_buy=False, current_price=current_price,
                reason="DATA_ERROR",
            )

        if df_1m is None or len(df_1m) < _VOL_SMA_PERIOD + 1:
            # 신규 상장 등으로 데이터가 부족한 종목은 안전하게 스킵
            self._buy_stats["data_error"] += 1
            return BuySignal(
                ticker=ticker, should_buy=False, current_price=current_price,
                reason="INSUFFICIENT_1M_DATA",
            )

        # 현재(최신) 1분봉 OHLCV 추출
        cur          = df_1m.iloc[-1]
        candle_open  = float(cur["open"])
        candle_high  = float(cur["high"])
        candle_low   = float(cur["low"])
        candle_close = current_price    # 실시간 현재가 사용 (WebSocket 가격)

        # 직전 20개 1분봉 평균 거래량 (현재봉 제외하여 미완성 봉 노이즈 차단)
        vol_sma20 = float(df_1m["volume"].iloc[-_VOL_SMA_PERIOD - 1:-1].mean())
        vol_cur   = float(cur["volume"])

        # 일봉 시초가 (오늘의 시가)
        daily_open = float(df_day.iloc[-1]["open"]) if not df_day.empty else current_price

        # ── [1] 거래량 폭발 필터 ─────────────────────────────────────────────
        # 직전 20분 평균의 N배 이상이어야 진짜 폭발적 매수 관심이 몰린 것
        # vol_mult가 너무 낮으면 일반 변동성도 감지되므로 15배(기본) 이상 유지 권장
        vol_ratio = vol_cur / vol_sma20 if vol_sma20 > 0 else 0.0
        if vol_ratio < _VOL_MULT:
            self._buy_stats["no_vol_spike"] += 1
            return BuySignal(
                ticker=ticker, should_buy=False, current_price=current_price,
                reason=f"VOL_WEAK({vol_ratio:.1f}x<{_VOL_MULT:.0f}x)",
            )

        # ── [2] 단기 가격 급등 필터 ──────────────────────────────────────────
        # 현재 1분봉 시가 대비 N% 이상 상승 중이어야 진짜 펌핑 신호
        # 거래량만 많고 가격이 안 올랐으면 세력의 물량 소화 구간일 수 있음
        spike_pct_actual = (candle_close - candle_open) / candle_open * 100 if candle_open > 0 else 0.0
        if spike_pct_actual < _SPIKE_PCT:
            self._buy_stats["no_spike"] += 1
            return BuySignal(
                ticker=ticker, should_buy=False, current_price=current_price,
                reason=f"NO_SPIKE({spike_pct_actual:.2f}%<{_SPIKE_PCT:.1f}%)",
            )

        # ── [3] 고점 추격 매수 방지 필터 ─────────────────────────────────────
        # 일봉 시가 대비 이미 N% 이상 올랐으면 '상투' 가능성이 높아 패스
        # 초기 펌핑에 올라타는 게 목적이므로, 이미 많이 오른 종목은 제외
        gain_from_daily = (current_price - daily_open) / daily_open * 100 if daily_open > 0 else 0.0
        if gain_from_daily > _MAX_GAIN_OPEN_PCT:
            self._buy_stats["overextended"] += 1
            return BuySignal(
                ticker=ticker, should_buy=False, current_price=current_price,
                reason=f"OVEREXTENDED({gain_from_daily:.1f}%>{_MAX_GAIN_OPEN_PCT:.0f}%)",
            )

        # ── [4] 양봉 품질 필터 (설거지 위꼬리 차단) ──────────────────────────
        # (현재가 - 시가) / (고가 - 저가) 비율이 낮으면
        # → 위꼬리만 길고 몸통이 작은 설거지/덤핑 봉 → 패스
        # 예: 거래량 폭발했지만 바로 되돌림이 일어난 '유동성 사냥' 패턴 방지
        candle_range = candle_high - candle_low
        if candle_range > 0:
            body_ratio = (candle_close - candle_open) / candle_range
            if body_ratio < _MIN_BODY_RATIO:
                self._buy_stats["weak_body"] += 1
                return BuySignal(
                    ticker=ticker, should_buy=False, current_price=current_price,
                    reason=f"WEAK_BODY(body={body_ratio:.2f}<{_MIN_BODY_RATIO:.1f})",
                )

        # ── [5] RSI 과열 방지 필터 ───────────────────────────────────────────
        # RSI(7, 1m) 이 극단적 과매수(78+)이면 이미 천장권 → 진입 금지
        # 조회 실패(신규 상장 등) 시에는 필터 우회
        rsi_1m: float | None = None
        try:
            rsi_1m = self._md.compute_rsi_intraday(ticker, period=7, interval="minute1")
            if rsi_1m > _RSI_MAX:
                self._buy_stats["rsi_hot"] += 1
                return BuySignal(
                    ticker=ticker, should_buy=False, current_price=current_price,
                    reason=f"RSI_HOT({rsi_1m:.1f}>{_RSI_MAX:.0f})",
                )
        except Exception:
            pass  # 데이터 부족 → 우회

        # ── [5b] RSI 하락 전환 감지 ──────────────────────────────────────────
        # 현재 RSI가 직전봉 RSI보다 3포인트 이상 낮으면 이미 모멘텀이 꺾인 것
        # → 고점 직후 뒤늦은 진입 차단 (slippage + 즉시 손절 패턴 방지)
        if rsi_1m is not None and len(df_1m) >= _VOL_SMA_PERIOD + 2:
            try:
                prev_closes = [float(c) for c in df_1m["close"].values[:-1]]
                prev_rsi = _rsi_last(prev_closes, period=7)
                if prev_rsi is not None and rsi_1m < prev_rsi - 3.0:
                    self._buy_stats["rsi_hot"] += 1
                    return BuySignal(
                        ticker=ticker, should_buy=False, current_price=current_price,
                        reason=f"RSI_FALLING({prev_rsi:.1f}→{rsi_1m:.1f})",
                    )
            except Exception:
                pass  # 계산 실패 시 우회

        # ── [6] 이미 하락 전환 중 필터 ───────────────────────────────────────
        # 현재가가 당 1분봉 고가의 95% 미만 → 펌핑이 지나간 뒤 하락 중
        # 뒤늦은 추격 매수를 차단하는 가장 현실적인 필터
        if candle_high > 0 and current_price < candle_high * 0.95:
            self._buy_stats["price_fading"] += 1
            return BuySignal(
                ticker=ticker, should_buy=False, current_price=current_price,
                reason=f"PRICE_FADING({current_price / candle_high:.3f}<0.95)",
            )

        # ── 모든 필터 통과 → 매수 신호 ──────────────────────────────────────
        self._buy_stats["passed"] += 1
        rsi_str = f"{rsi_1m:.1f}" if rsi_1m is not None else "N/A"
        reason = (
            f"PUMP_DETECTED("
            f"vol={vol_ratio:.1f}x"
            f"|spike={spike_pct_actual:.2f}%"
            f"|daily_gain={gain_from_daily:.1f}%"
            f"|rsi={rsi_str})"
        )
        logger.info(f"[pump_catcher] ★ 매수 신호 | {ticker} | {reason}")

        return BuySignal(
            ticker=ticker,
            should_buy=True,
            current_price=current_price,
            reason=reason,
            metadata={
                "vol_ratio":       round(vol_ratio, 1),
                "spike_pct":       round(spike_pct_actual, 2),
                "gain_from_daily": round(gain_from_daily, 2),
                "rsi_1m":          round(rsi_1m, 1) if rsi_1m is not None else None,
                "stop_loss_pct":   _HARD_SL_PCT,   # risk_manager가 SL가를 계산할 때 참조
                "tp_price":        round(current_price * (1 + _TP_LOCK_PCT / 100), 0),
                "tp_label":        "수익보존락",
            },
        )

    # ─── 매도 ────────────────────────────────────────────────────────────────

    def should_sell_on_signal(
        self, ticker: str, current_price: float, position
    ) -> SellSignal:
        entry   = position.buy_price
        pnl_pct = (current_price - entry) / entry * 100

        # ── 최고가 갱신 (트레일링 스탑 기준) ─────────────────────────────────
        peak = self._peaks.get(ticker, current_price)
        if current_price > peak:
            peak = current_price
        self._peaks[ticker] = peak

        peak_pnl_pct = (peak - entry) / entry * 100

        # ── 수익 보존 락: peak가 tp_lock_pct%에 도달하면 trail을 좁힌다 ──────
        # 5% 수익이 났다면 1% 되돌림만 허용하여 최소 4%를 확정
        if peak_pnl_pct >= _TP_LOCK_PCT and ticker not in self._tp_locked:
            self._tp_locked.add(ticker)
            logger.info(
                f"[pump_catcher] TP 락 활성화 | {ticker} | "
                f"peak={peak_pnl_pct:+.2f}% → trail {_TRAIL_PCT}%→{_TRAIL_LOCKED_PCT}%"
            )

        # 현재 적용할 트레일링 폭 (수익 보존 락 활성 여부에 따라)
        effective_trail = _TRAIL_LOCKED_PCT if ticker in self._tp_locked else _TRAIL_PCT

        # ── [1순위] 하드 손절: 진입가 대비 -N% → 즉시 청산 ─────────────────
        # 트레일링보다 우선 적용. 설거지/급락 물림을 단호하게 차단.
        if pnl_pct <= -_HARD_SL_PCT:
            reason = f"HARD_SL(pnl={pnl_pct:+.2f}%<=-{_HARD_SL_PCT}%)"
            logger.info(
                f"[pump_catcher] ★ 하드 손절 | {ticker} | "
                f"entry={entry:,.0f} now={current_price:,.0f} | {reason}"
            )
            self.on_position_closed(ticker)
            return SellSignal(ticker, True, current_price, reason)

        # ── [2순위] 트레일링 스탑: peak에서 N% 하락 → 청산 ─────────────────
        # peak PnL이 0% 초과인 경우에만 트레일링 적용
        # (아직 손해 구간이면 하드 SL이 담당)
        if peak_pnl_pct > 0:
            drop_from_peak = (peak - current_price) / peak * 100
            if drop_from_peak >= effective_trail:
                locked_str = "LOCKED" if ticker in self._tp_locked else "NORMAL"
                reason = (
                    f"TRAIL_STOP("
                    f"peak={peak_pnl_pct:+.2f}%"
                    f"|drop={drop_from_peak:.2f}%>={effective_trail:.1f}%"
                    f"|pnl={pnl_pct:+.2f}%|{locked_str})"
                )
                logger.info(
                    f"[pump_catcher] ★ 트레일링 청산 | {ticker} | "
                    f"entry={entry:,.0f} peak={peak:,.0f} now={current_price:,.0f} | {reason}"
                )
                self.on_position_closed(ticker)
                return SellSignal(ticker, True, current_price, reason)

        # ── [3순위] 거래량 소멸 청산 ─────────────────────────────────────────
        # 펌핑을 이끈 비정상 거래량이 다시 평범해지면 모멘텀 종료를 뜻함
        # 수익이 2% 미만인 상태에서 거래량이 SMA의 N배 이하로 떨어지면 탈출
        # v3: 1.0→2.0% (모멘텀 소멸 시 더 빨리 이탈하여 손실 확대 방지)
        if pnl_pct < 2.0:
            try:
                vol_now, vol_sma = self._md.compute_volume_sma_intraday(
                    ticker, _VOL_SMA_PERIOD, "minute1"
                )
                if vol_sma > 0 and vol_now < vol_sma * _VOL_FADE_MULT:
                    reason = (
                        f"VOL_FADE("
                        f"vol={vol_now:.0f}<sma*{_VOL_FADE_MULT:.0f}={vol_sma * _VOL_FADE_MULT:.0f}"
                        f"|pnl={pnl_pct:+.2f}%)"
                    )
                    logger.info(
                        f"[pump_catcher] ★ 거래량 소멸 청산 | {ticker} | {reason}"
                    )
                    self.on_position_closed(ticker)
                    return SellSignal(ticker, True, current_price, reason)
            except Exception:
                pass  # 조회 실패 시 스킵 (다음 순위로)

        # ── [4순위] 타임컷: N분 보유 후 수익 미달 → 탈출 ────────────────────
        # 진짜 펌핑이면 10분 안에 터진다. 그 이상 기다려도 수익이 없으면
        # 잘못 잡은 것이므로 손실이 커지기 전에 자른다.
        try:
            buy_time = position.buy_time
            if isinstance(buy_time, str):
                buy_time = datetime.fromisoformat(buy_time)
            if buy_time.tzinfo is None:
                buy_time = buy_time.replace(tzinfo=_KST)
            elapsed_min = (datetime.now(_KST) - buy_time).total_seconds() / 60

            if elapsed_min >= _MAX_HOLD_MIN and pnl_pct < 1.0:
                reason = (
                    f"TIME_CUT("
                    f"{elapsed_min:.1f}min>={_MAX_HOLD_MIN:.0f}min"
                    f"|pnl={pnl_pct:+.2f}%)"
                )
                logger.info(
                    f"[pump_catcher] ★ 타임컷 청산 | {ticker} | "
                    f"entry={entry:,.0f} now={current_price:,.0f} | {reason}"
                )
                self.on_position_closed(ticker)
                return SellSignal(ticker, True, current_price, reason)
        except Exception as e:
            logger.warning(f"[pump_catcher] 타임컷 계산 오류: {e}")

        # 아직 청산 조건 미충족 → 보유 유지
        return SellSignal(ticker, False, current_price, "")
