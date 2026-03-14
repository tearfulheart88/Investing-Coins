"""
전략: 모멘텀 스카우트 — 급등 직전 코인 사전 감지 (Pre-Pump Momentum Scout)
시나리오 ID: momentum_scout

■ 전략 개요
  - 급등이 시작되기 직전의 '준비 신호'를 포착: BB 수렴(에너지 응축) + 거래량 누적 + RSI 상승 전환
  - pump_catcher가 이미 터진 펌핑에 올라타는 반면, 이 전략은 터지기 직전에 선진입
  - 5분봉 기준, 더 넓은 스탑(3.5%)과 더 긴 보유(90분)으로 급등 전체를 수익화
  - 150개 이상 종목 스캔 → 적은 신호지만 고승률·고수익성 목표

■ 매수 조건 (AND 7개)
  1. 고점 추격 방지:  일봉 시가 대비 상승률 <= max_gain_from_open(12%)
  2. BB 수렴:         5m BB 밴드폭(%) < bb_squeeze_pct(3.5%) — 변동성 압축, 폭발 준비
  3. 가격 위치:       현재가가 BB 구간 하위 65% 이내 — 오버슈팅 전 단계
  4. 거래량 누적:     5m 거래량 >= SMA20(5m) × vol_buildup_mult(2.5×) — 점진적 세력 유입
  5. RSI 포지셔닝:   rsi_min(38) <= RSI(14, 5m) <= rsi_max(62) — 과열/침체 구간 제외
  6. RSI 상승세:      현재 RSI > 직전봉 RSI + 1.0 — 모멘텀 상승 전환 확인
  7. 추세 필터:       현재가 >= SMA20(5m) × 0.985 — 강한 하락추세 제외

■ 매도 조건 (우선순위순)
  1. 하드 손절:        진입가 대비 -hard_sl_pct%(-3.5%) → 즉시 청산
  2. 수익 보존 락:     peak >= tp_lock_pct(4.0%) 시 trail을 trail_locked_pct(1.5%)로 축소
  3. 트레일링 스탑:    peak에서 trail_pct(3.0%) 이상 하락 → 청산
  4. BB 상단 돌파 후 반전: 가격이 BB 상단 위로 올랐다가 BB 중심선 아래로 복귀 → 청산
  5. 타임컷:           max_hold_minutes(90분) 초과 + PnL < 0.5% → 청산

■ 주요 파라미터
  bb_squeeze_pct    = 3.5%   BB 밴드폭 수렴 임계값 (좁을수록 강한 응축)
  vol_buildup_mult  = 2.5    거래량 누적 배수 (pump_catcher의 12x보다 훨씬 낮음 = 사전 감지)
  rsi_min / rsi_max = 38/62  RSI 유효 범위 (중립~초기 강세)
  hard_sl_pct       = 3.5%   하드 손절 (선진입이라 슬리피지 여유 포함)
  tp_lock_pct       = 4.0%   수익 보존 락 발동 기준
  trail_pct         = 3.0%   기본 트레일링 스탑 폭
  trail_locked_pct  = 1.5%   수익 보존 후 좁혀진 트레일링 폭
  max_hold_minutes  = 90     최대 보유 시간 (분) — 선진입이라 pump보다 여유
  cooldown_minutes  = 60     동일 종목 재진입 쿨다운 (분)
  max_gain_from_open= 12%    일봉 시가 대비 최대 허용 상승률

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

# ─── 모듈 상수 (config.STRATEGY_PARAMS["momentum_scout"]에서 초기화) ─────────
_ms = config.STRATEGY_PARAMS.get("momentum_scout", {})
_BB_PERIOD          = int(_ms.get("bb_period",            20))
_BB_STD             = float(_ms.get("bb_std",              2.0))
_BB_SQUEEZE_PCT     = float(_ms.get("bb_squeeze_pct",      3.5))   # bandwidth% 임계값
_VOL_BUILDUP_MULT   = float(_ms.get("vol_buildup_mult",    2.5))   # 거래량 누적 배수
_RSI_MIN            = float(_ms.get("rsi_min",            38.0))
_RSI_MAX            = float(_ms.get("rsi_max",            62.0))
_HARD_SL_PCT        = float(_ms.get("hard_sl_pct",         3.5))
_TP_LOCK_PCT        = float(_ms.get("tp_lock_pct",         4.0))
_TRAIL_PCT          = float(_ms.get("trail_pct",           3.0))
_TRAIL_LOCKED_PCT   = float(_ms.get("trail_locked_pct",    1.5))
_MAX_HOLD_MIN       = float(_ms.get("max_hold_minutes",   90.0))
_COOLDOWN_MIN       = float(_ms.get("cooldown_minutes",   60.0))
_MAX_GAIN_OPEN_PCT  = float(_ms.get("max_gain_from_open", 12.0))

_VOL_SMA_PERIOD = 20          # 거래량 SMA 기간 (5분봉 기준 = 100분 평균)
_INTERVAL       = "minute5"   # 기본 분봉 타임프레임
_KST            = timezone(timedelta(hours=9))


def _rsi_prev(closes: list, period: int = 14) -> float | None:
    """
    closes 리스트 마지막-1 지점의 Wilder RSI(period) 계산.
    RSI 기울기(상승/하락) 판정용 — 직전봉 RSI와 현재봉 RSI 비교에 사용.
    데이터 부족 시 None 반환.
    """
    if len(closes) < period + 2:
        return None
    gains: list[float] = []
    losses: list[float] = []
    for i in range(1, len(closes)):
        diff = closes[i] - closes[i - 1]
        gains.append(max(diff, 0.0))
        losses.append(max(-diff, 0.0))
    # Wilder 초기 평균 (period개 기준)
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    # Wilder 평활 — 마지막 값(현재봉)은 제외하고 직전봉까지만
    for i in range(period, len(gains) - 1):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    if avg_loss == 0:
        return 100.0
    return 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)


class MomentumScoutStrategy(BaseStrategy):
    """
    급등 직전 코인 사전 감지 전략.
    BB 수렴 + 거래량 누적 + RSI 상승 전환을 동시에 감지하여 펌핑 직전 선진입.
    pump_catcher와 달리 이미 터진 펌핑이 아니라 에너지 응축 구간에서 매수.
    """

    def __init__(self, market_data: MarketData) -> None:
        self._md = market_data

        # peak 추적: ticker → 최고가 (트레일링 스탑용)
        self._peaks: dict[str, float] = {}

        # 수익 보존 락: ticker → True (tp_lock_pct 이상 달성 시 활성)
        self._tp_locked: set[str] = set()

        # BB 상단 돌파 추적: ticker → True (한 번이라도 upper를 돌파했는지)
        self._bb_breached: set[str] = set()

        # 쿨다운 추적: ticker → 마지막 매도 timestamp
        self._cooldown_map: dict[str, float] = {}

        # 매수 필터 통계 (일별 로깅용)
        self._buy_stats: dict[str, int] = {
            "total":               0,
            "cooldown":            0,   # 쿨다운 중
            "overextended":        0,   # 일봉 시가 대비 너무 많이 오름
            "no_squeeze":          0,   # BB 수렴 미달 (밴드폭 충분히 좁지 않음)
            "price_high_in_band":  0,   # 가격이 밴드 상단 35% 이내 (오버슈팅 중)
            "no_vol_buildup":      0,   # 거래량 누적 미달
            "rsi_weak":            0,   # RSI 너무 낮음 (침체 구간)
            "rsi_hot":             0,   # RSI 너무 높음 (과열 구간)
            "rsi_flat":            0,   # RSI 상승 전환 미확인 (기울기 부족)
            "below_sma":           0,   # SMA20 아래 (하락 추세)
            "data_error":          0,   # 데이터 조회 오류
            "passed":              0,   # 모든 필터 통과 → 매수 신호 발생
        }

    # ─── 전략 ID ─────────────────────────────────────────────────────────────

    def get_strategy_id(self) -> str:
        return "scalping"

    def get_scenario_id(self) -> str:
        return "momentum_scout"

    def requires_scheduled_sell(self) -> bool:
        # 자체 청산 로직만 사용 (09:00 스케줄 매도 없음)
        return False

    def get_history_requirements(self) -> dict[str, int]:
        # BB(20) + SMA(20) 계산을 위해 5분봉 50개 이상 필요 (= 약 4시간 데이터)
        return {
            "day": 2,
            "minute5": max(_BB_PERIOD + _VOL_SMA_PERIOD, 50),
        }

    def get_ticker_selection_profile(self) -> dict:
        # 많은 종목 스캔이 핵심 — 급등 준비 코인은 어디서 나올지 모름
        return {
            "pattern": "pump_event",      # 거래대금 변동성 높은 코인 우선
            "pool_size": 150,             # 150개 종목 스캔 (pump_catcher 180개보다 약간 적게)
            "refresh_hours": 0.5,         # 30분마다 종목 갱신
        }

    # ─── 포지션 종료 콜백 ─────────────────────────────────────────────────────

    def on_position_closed(self, ticker: str, reason: str = "") -> None:
        """포지션 종료 시 내부 상태 정리 + 쿨다운 시작."""
        self._peaks.pop(ticker, None)
        self._tp_locked.discard(ticker)
        self._bb_breached.discard(ticker)
        self._cooldown_map[ticker] = time.time()

    def reset_daily(self) -> None:
        """09:00 이후 일별 통계 로깅 + 상태 초기화."""
        if self._buy_stats["total"] > 0:
            logger.info(
                f"[momentum_scout] 일별 매수필터 통계 | "
                f"총={self._buy_stats['total']} "
                f"쿨다운={self._buy_stats['cooldown']} "
                f"고점추격={self._buy_stats['overextended']} "
                f"BB미수렴={self._buy_stats['no_squeeze']} "
                f"가격오버슈팅={self._buy_stats['price_high_in_band']} "
                f"거래량낮음={self._buy_stats['no_vol_buildup']} "
                f"RSI침체={self._buy_stats['rsi_weak']} "
                f"RSI과열={self._buy_stats['rsi_hot']} "
                f"RSI기울기없음={self._buy_stats['rsi_flat']} "
                f"SMA아래={self._buy_stats['below_sma']} "
                f"에러={self._buy_stats['data_error']} "
                f"통과={self._buy_stats['passed']}"
            )
        self._peaks.clear()
        self._tp_locked.clear()
        self._bb_breached.clear()
        # 쿨다운은 일 경계를 넘어도 유효하게 유지
        for k in self._buy_stats:
            self._buy_stats[k] = 0

    # ─── 매수 ────────────────────────────────────────────────────────────────

    def should_buy(self, ticker: str, current_price: float) -> BuySignal:
        self._buy_stats["total"] += 1

        # ── [0] 쿨다운 체크 ──────────────────────────────────────────────────
        last_sell_ts = self._cooldown_map.get(ticker)
        if last_sell_ts and (time.time() - last_sell_ts) < _COOLDOWN_MIN * 60:
            self._buy_stats["cooldown"] += 1
            elapsed_min = (time.time() - last_sell_ts) / 60
            return BuySignal(
                ticker=ticker, should_buy=False, current_price=current_price,
                reason=f"COOLDOWN({elapsed_min:.0f}min<{_COOLDOWN_MIN:.0f}min)",
            )

        # ── 데이터 수집 ──────────────────────────────────────────────────────
        try:
            # 5분봉: BB + SMA + 거래량 계산을 위해 최소 (bb_period + vol_period + 여유) 봉
            df_5m  = self._md.get_ohlcv_intraday(
                ticker, interval=_INTERVAL, count=_BB_PERIOD + _VOL_SMA_PERIOD + 10
            )
            # 일봉: 오늘 시가(open) 확인용
            df_day = self._md.get_ohlcv(ticker, count=2)
        except DataFetchError as e:
            self._buy_stats["data_error"] += 1
            logger.debug(f"[momentum_scout] 데이터 조회 실패: {ticker}: {e}")
            return BuySignal(
                ticker=ticker, should_buy=False, current_price=current_price,
                reason="DATA_ERROR",
            )

        if df_5m is None or len(df_5m) < _VOL_SMA_PERIOD + 1:
            self._buy_stats["data_error"] += 1
            return BuySignal(
                ticker=ticker, should_buy=False, current_price=current_price,
                reason="INSUFFICIENT_5M_DATA",
            )

        # ── 일봉 시가 대비 상승률 ─────────────────────────────────────────────
        daily_open = float(df_day.iloc[-1]["open"]) if not df_day.empty else current_price
        gain_from_daily = (
            (current_price - daily_open) / daily_open * 100 if daily_open > 0 else 0.0
        )

        # ── [1] 고점 추격 방지 ────────────────────────────────────────────────
        if gain_from_daily > _MAX_GAIN_OPEN_PCT:
            self._buy_stats["overextended"] += 1
            return BuySignal(
                ticker=ticker, should_buy=False, current_price=current_price,
                reason=f"OVEREXTENDED({gain_from_daily:.1f}%>{_MAX_GAIN_OPEN_PCT:.0f}%)",
            )

        # ── 볼린저 밴드 계산 (5분봉) ──────────────────────────────────────────
        try:
            bb_upper, bb_mid, bb_lower = self._md.compute_bollinger_intraday(
                ticker, period=_BB_PERIOD, std_mult=_BB_STD, interval=_INTERVAL
            )
        except DataFetchError:
            self._buy_stats["data_error"] += 1
            return BuySignal(
                ticker=ticker, should_buy=False, current_price=current_price,
                reason="BB_ERROR",
            )

        # 밴드폭 (%): (upper - lower) / mid × 100
        bb_band_range = bb_upper - bb_lower
        bandwidth_pct = bb_band_range / bb_mid * 100 if bb_mid > 0 else 999.0

        # ── [2] BB 수렴 필터 ──────────────────────────────────────────────────
        # 밴드폭이 충분히 좁아야 에너지 응축 상태. 너무 넓으면 이미 추세 진행 중.
        if bandwidth_pct >= _BB_SQUEEZE_PCT:
            self._buy_stats["no_squeeze"] += 1
            return BuySignal(
                ticker=ticker, should_buy=False, current_price=current_price,
                reason=f"NO_SQUEEZE(bw={bandwidth_pct:.2f}%>={_BB_SQUEEZE_PCT:.1f}%)",
            )

        # ── [3] 가격 위치 필터 ────────────────────────────────────────────────
        # 밴드 내 상대 위치: 0.0 = 하단, 1.0 = 상단
        # 상단 35% 구간(position > 0.65)이면 이미 오버슈팅 중 → 패스
        price_in_band = (
            (current_price - bb_lower) / bb_band_range if bb_band_range > 0 else 0.5
        )
        if price_in_band > 0.65:
            self._buy_stats["price_high_in_band"] += 1
            return BuySignal(
                ticker=ticker, should_buy=False, current_price=current_price,
                reason=f"PRICE_HIGH_IN_BAND(pos={price_in_band:.2f}>0.65)",
            )

        # ── 거래량 SMA 계산 ────────────────────────────────────────────────────
        try:
            vol_cur, vol_sma = self._md.compute_volume_sma_intraday(
                ticker, _VOL_SMA_PERIOD, _INTERVAL
            )
        except DataFetchError:
            self._buy_stats["data_error"] += 1
            return BuySignal(
                ticker=ticker, should_buy=False, current_price=current_price,
                reason="VOL_ERROR",
            )

        vol_ratio = vol_cur / vol_sma if vol_sma > 0 else 0.0

        # ── [4] 거래량 누적 필터 ──────────────────────────────────────────────
        # SMA의 2.5배 이상이어야 세력 유입 조짐. pump_catcher의 12배보다 훨씬 낮음.
        if vol_ratio < _VOL_BUILDUP_MULT:
            self._buy_stats["no_vol_buildup"] += 1
            return BuySignal(
                ticker=ticker, should_buy=False, current_price=current_price,
                reason=f"VOL_LOW({vol_ratio:.1f}x<{_VOL_BUILDUP_MULT:.1f}x)",
            )

        # ── RSI 계산 (5분봉) ────────────────────────────────────────────────────
        rsi: float | None = None
        try:
            rsi = self._md.compute_rsi_intraday(ticker, period=14, interval=_INTERVAL)
        except Exception:
            pass  # 데이터 부족 → 필터 우회

        # ── [5] RSI 범위 필터 ──────────────────────────────────────────────────
        if rsi is not None:
            if rsi < _RSI_MIN:
                self._buy_stats["rsi_weak"] += 1
                return BuySignal(
                    ticker=ticker, should_buy=False, current_price=current_price,
                    reason=f"RSI_LOW({rsi:.1f}<{_RSI_MIN:.0f})",
                )
            if rsi > _RSI_MAX:
                self._buy_stats["rsi_hot"] += 1
                return BuySignal(
                    ticker=ticker, should_buy=False, current_price=current_price,
                    reason=f"RSI_HIGH({rsi:.1f}>{_RSI_MAX:.0f})",
                )

        # ── [6] RSI 상승 기울기 필터 ──────────────────────────────────────────
        # 현재 RSI가 직전봉 RSI보다 1포인트 이상 높아야 모멘텀 상승 전환 신호
        # 평평하거나 하락 중인 RSI는 아직 '불씨'가 없음
        if rsi is not None:
            try:
                closes_list = [float(c) for c in df_5m["close"].values]
                rsi_prev_val = _rsi_prev(closes_list, period=14)
                if rsi_prev_val is not None and rsi <= rsi_prev_val + 1.0:
                    self._buy_stats["rsi_flat"] += 1
                    return BuySignal(
                        ticker=ticker, should_buy=False, current_price=current_price,
                        reason=f"RSI_FLAT({rsi_prev_val:.1f}→{rsi:.1f})",
                    )
            except Exception:
                pass  # 계산 실패 시 우회

        # ── [7] SMA 필터 ───────────────────────────────────────────────────────
        # 5분봉 SMA(20) 대비 1.5% 이상 아래면 하락 추세 → 패스
        closes_5m = df_5m["close"].dropna()
        sma20 = float(closes_5m.iloc[-_BB_PERIOD:].mean()) if len(closes_5m) >= _BB_PERIOD else 0.0
        if sma20 > 0 and current_price < sma20 * 0.985:
            self._buy_stats["below_sma"] += 1
            return BuySignal(
                ticker=ticker, should_buy=False, current_price=current_price,
                reason=f"BELOW_SMA({current_price / sma20:.3f}<0.985)",
            )

        # ── 모든 필터 통과 → 매수 신호 ──────────────────────────────────────
        self._buy_stats["passed"] += 1
        rsi_str = f"{rsi:.1f}" if rsi is not None else "N/A"
        reason = (
            f"PRE_PUMP("
            f"bw={bandwidth_pct:.2f}%"
            f"|pos={price_in_band:.2f}"
            f"|vol={vol_ratio:.1f}x"
            f"|rsi={rsi_str}"
            f"|daily={gain_from_daily:.1f}%)"
        )
        logger.info(f"[momentum_scout] ★ 매수 신호 | {ticker} | {reason}")

        return BuySignal(
            ticker=ticker,
            should_buy=True,
            current_price=current_price,
            reason=reason,
            metadata={
                "bandwidth_pct":    round(bandwidth_pct, 2),
                "price_in_band":    round(price_in_band, 2),
                "vol_ratio":        round(vol_ratio, 1),
                "rsi_5m":           round(rsi, 1) if rsi is not None else None,
                "gain_from_daily":  round(gain_from_daily, 2),
                "bb_upper":         round(bb_upper, 0),
                "bb_mid":           round(bb_mid, 0),
                "stop_loss_pct":    _HARD_SL_PCT,
                "tp_price":         round(current_price * (1 + _TP_LOCK_PCT / 100), 0),
                "tp_label":         "수익보존락",
            },
        )

    # ─── 매도 ────────────────────────────────────────────────────────────────

    def should_sell_on_signal(
        self, ticker: str, current_price: float, position
    ) -> SellSignal:
        entry   = position.buy_price
        pnl_pct = (current_price - entry) / entry * 100

        # ── 최고가 갱신 (트레일링 기준점) ─────────────────────────────────────
        peak = self._peaks.get(ticker, current_price)
        if current_price > peak:
            peak = current_price
        self._peaks[ticker] = peak

        peak_pnl_pct = (peak - entry) / entry * 100

        # ── 수익 보존 락: peak >= tp_lock_pct% 도달 시 trail 폭 축소 ──────────
        if peak_pnl_pct >= _TP_LOCK_PCT and ticker not in self._tp_locked:
            self._tp_locked.add(ticker)
            logger.info(
                f"[momentum_scout] TP 락 활성화 | {ticker} | "
                f"peak={peak_pnl_pct:+.2f}% → trail {_TRAIL_PCT}%→{_TRAIL_LOCKED_PCT}%"
            )

        effective_trail = _TRAIL_LOCKED_PCT if ticker in self._tp_locked else _TRAIL_PCT

        # ── [1순위] 하드 손절 ─────────────────────────────────────────────────
        if pnl_pct <= -_HARD_SL_PCT:
            reason = f"HARD_SL(pnl={pnl_pct:+.2f}%<=-{_HARD_SL_PCT}%)"
            logger.info(
                f"[momentum_scout] ★ 하드 손절 | {ticker} | "
                f"entry={entry:,.0f} now={current_price:,.0f} | {reason}"
            )
            self.on_position_closed(ticker)
            return SellSignal(ticker, True, current_price, reason)

        # ── [2순위] 트레일링 스탑 ────────────────────────────────────────────
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
                    f"[momentum_scout] ★ 트레일링 청산 | {ticker} | "
                    f"entry={entry:,.0f} peak={peak:,.0f} now={current_price:,.0f} | {reason}"
                )
                self.on_position_closed(ticker)
                return SellSignal(ticker, True, current_price, reason)

        # ── [3순위] BB 상단 돌파 후 반전 ─────────────────────────────────────
        # 선진입 전략의 핵심 청산 로직:
        # BB 수렴 → 급등(BB 상단 돌파) → 기세 꺾임(BB 중심선 아래 복귀) → 탈출
        try:
            bb_upper, bb_mid, _ = self._md.compute_bollinger_intraday(
                ticker, period=_BB_PERIOD, std_mult=_BB_STD, interval=_INTERVAL
            )
            # 한 번이라도 BB 상단 돌파 시 플래그 세팅
            if current_price >= bb_upper:
                self._bb_breached.add(ticker)
            # BB 상단을 돌파한 적 있고, 현재 가격이 BB 중심선 아래로 내려오면 청산
            if ticker in self._bb_breached and current_price < bb_mid:
                reason = (
                    f"BB_REVERSION("
                    f"price={current_price:,.0f}<mid={bb_mid:,.0f}"
                    f"|pnl={pnl_pct:+.2f}%)"
                )
                logger.info(
                    f"[momentum_scout] ★ BB 반전 청산 | {ticker} | "
                    f"entry={entry:,.0f} now={current_price:,.0f} | {reason}"
                )
                self.on_position_closed(ticker)
                return SellSignal(ticker, True, current_price, reason)
        except Exception:
            pass  # BB 계산 실패 시 스킵

        # ── [4순위] 타임컷 ───────────────────────────────────────────────────
        # 선진입 후 충분히 기다렸음에도 의미 있는 수익이 없으면 탈출
        try:
            buy_time = position.buy_time
            if isinstance(buy_time, str):
                buy_time = datetime.fromisoformat(buy_time)
            if buy_time.tzinfo is None:
                buy_time = buy_time.replace(tzinfo=_KST)
            elapsed_min = (datetime.now(_KST) - buy_time).total_seconds() / 60

            if elapsed_min >= _MAX_HOLD_MIN and pnl_pct < 0.5:
                reason = (
                    f"TIME_CUT("
                    f"{elapsed_min:.0f}min>={_MAX_HOLD_MIN:.0f}min"
                    f"|pnl={pnl_pct:+.2f}%)"
                )
                logger.info(
                    f"[momentum_scout] ★ 타임컷 청산 | {ticker} | "
                    f"entry={entry:,.0f} now={current_price:,.0f} | {reason}"
                )
                self.on_position_closed(ticker)
                return SellSignal(ticker, True, current_price, reason)
        except Exception as e:
            logger.warning(f"[momentum_scout] 타임컷 계산 오류: {e}")

        # 청산 조건 미충족 → 보유 유지
        return SellSignal(ticker, False, current_price, "")
