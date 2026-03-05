"""
전략: 5분봉 삼중 EMA 기반 추세 눌림목 전략 (개선판 v3)
시나리오 ID: scalping_triple_ema

■ 개선 사항 (v3) — 2026-03-05 일보 기반
  - 타임프레임 업그레이드: 1분봉 → 5분봉 (78건/5.3%승률 → 1분봉 노이즈 제거)
  - HTF 비례 상향: 15분봉 → 1시간봉 (상위 추세 필터)
  - SL 상향: 0.3% → 1.5% (5분봉 정상 변동 수용)
  - TP 기준 상향: 0.6% → 2.0% (5분봉 스윙 스케일에 맞춤)
  - 대형 음봉 필터: 직전 캔들이 비정상적 큰 음봉이면 매수 금지 (패닉셀 진입 방지)

■ 개선 사항 (v2)
  1. HTF 추세 필터  : 1시간봉 EMA(200) 위에서만 진입
  2. ADX 횡보 필터  : ADX(14, 5분봉) ≥ 20일 때만 진입
  3. EMA 이격도 필터: (EMA10 - EMA50) / EMA50 × 100 > EMA_SPREAD_MIN(0.3%)
  4. Trailing Stop  : 고정 TP 대신 Trailing Stop 적용
     - TP 50% 지점 도달 → 본절가로 SL 이동
     - 트레일링 폭 최소 TRAIL_MIN_PCT(1.5%) 적용
  5. 대형 음봉 필터 : 직전 캔들 body > ATR(20) × PANIC_BODY_MULT(2.5) → 매수 금지

EMA 주기: EMA(10) / EMA(20) / EMA(50)
매수 조건 (순서대로 모두 충족):
  1. 1시간봉 현재가 > EMA(200, 1h)         [상위 추세 필터]
  2. ADX(14, 5분봉) ≥ ADX_MIN(20)          [횡보장 필터]
  3. EMA(10) > EMA(20) > EMA(50)           [정배열 상승 추세]
  3a. (EMA10-EMA50)/EMA50×100 > 0.3%      [EMA 이격도 필터]
  3b. 직전 캔들 body ≤ ATR(20)×2.5        [대형 음봉 필터]
  4. 이전 캔들 종가 < EMA(10)              [눌림목 발생]
  5. 이전 캔들 저가 > EMA(20)              [EMA20 위에서 지지]
  6. 현재 캔들 종가 > EMA(10)              [재돌파 진입]

매도: Trailing Stop
  - 미활성(peak < TP50%): risk_manager 하드 스탑만 적용
  - 활성(peak ≥ TP50%): trail_stop = max(본절가, peak × (1 − TRAIL_MIN_PCT))
  - 현재가 ≤ trail_stop → 매도

타임프레임: 5분봉 (진입) + 1시간봉 (HTF 필터)
"""
import logging
from data.market_data import MarketData
from exchange.upbit_client import DataFetchError
from strategies.base_strategy import BaseStrategy, BuySignal, SellSignal

logger = logging.getLogger(__name__)

_INTERVAL     = "minute5"     # v3: 1분봉 → 5분봉 (노이즈 제거)
_HTF_INTERVAL = "minute60"    # v3: 15분봉 → 1시간봉 (HTF 비례 상향)
_HTF_EMA      = 200           # HTF EMA 기간 (상위 추세 필터)

_TP_PCT          = 0.02       # v3: 0.6% → 2.0% (5분봉 스윙 스케일)
_SL_PCT          = 0.015      # v3: 0.3% → 1.5% (5분봉 정상 변동 수용)
_TRAIL_MIN_PCT   = 0.015      # 트레일링 최소 폭 1.5%
_ADX_MIN         = 20.0       # ADX 최소 추세 강도
_EMA_SPREAD_MIN  = 0.3        # EMA10-EMA50 이격도 최소 기준 (%)
_PANIC_BODY_MULT = 2.5        # v3: 대형 음봉 판단 배수 (body > ATR(20) × 이 값)
_ATR_PERIOD      = 20         # ATR 기간 (대형 음봉 필터용)


class TripleEMAStrategy(BaseStrategy):

    def __init__(self, market_data: MarketData) -> None:
        self._md    = market_data
        self._peaks: dict[str, float] = {}   # ticker → 포지션 진입 후 최고가

    def get_strategy_id(self) -> str:
        return "scalping"

    def get_scenario_id(self) -> str:
        return "scalping_triple_ema"

    def requires_scheduled_sell(self) -> bool:
        return False

    # ─── 매수 신호 ────────────────────────────────────────────────────────────

    def should_buy(self, ticker: str, current_price: float) -> BuySignal:
        try:
            # 5분봉 EMA(10/20/50)
            ema_df  = self._md.compute_ema_df(ticker, [10, 20, 50], _INTERVAL)
            # 1시간봉 EMA(200) — HTF 추세 필터
            htf_df  = self._md.compute_ema_df(ticker, [_HTF_EMA], _HTF_INTERVAL)
            # 5분봉 ADX — 횡보 필터
            adx     = self._md.compute_adx(ticker, 14, _INTERVAL)
        except DataFetchError as e:
            logger.warning(f"[triple_ema] 데이터 오류: {ticker} - {e}")
            return BuySignal(ticker, False, current_price, "DATA_ERROR")

        if len(ema_df) < 3 or len(htf_df) < 1:
            return BuySignal(ticker, False, current_price, "DATA_INSUFFICIENT")

        cur  = ema_df.iloc[-1]
        prev = ema_df.iloc[-2]

        ema10 = float(cur["ema10"])
        ema20 = float(cur["ema20"])
        ema50 = float(cur["ema50"])
        ema_htf = float(htf_df.iloc[-1][f"ema{_HTF_EMA}"])

        meta = {
            "ema10": round(ema10, 0), "ema20": round(ema20, 0), "ema50": round(ema50, 0),
            f"ema{_HTF_EMA}_1h": round(ema_htf, 0), "adx_5m": round(adx, 1),
        }

        # ── 필터 1: HTF 추세 (현재가 > EMA200 1시간봉) ─────────────────────
        if current_price <= ema_htf:
            return BuySignal(ticker, False, current_price,
                             f"HTF_BELOW_EMA{_HTF_EMA}({current_price:.0f}<={ema_htf:.0f})",
                             metadata=meta)

        # ── 필터 2: ADX ≥ 추세 최소 기준 ────────────────────────────────────
        if adx < _ADX_MIN:
            return BuySignal(ticker, False, current_price,
                             f"ADX_WEAK({adx:.1f}<{_ADX_MIN})", metadata=meta)

        # ── 조건 3: 정배열 EMA10 > EMA20 > EMA50 ────────────────────────────
        if not (ema10 > ema20 > ema50):
            return BuySignal(ticker, False, current_price, "NO_UPTREND", metadata=meta)

        # ── 조건 3a: EMA 이격도 필터 ─────────────────────────────────────────
        ema_spread_pct = (ema10 - ema50) / ema50 * 100
        meta["ema_spread_pct"] = round(ema_spread_pct, 3)
        if ema_spread_pct <= _EMA_SPREAD_MIN:
            return BuySignal(ticker, False, current_price,
                             f"EMA_SPREAD_NARROW({ema_spread_pct:.3f}%<={_EMA_SPREAD_MIN}%)",
                             metadata=meta)

        # ── 조건 3b: 대형 음봉 필터 (패닉셀 진입 방지) ──────────────────────
        prev_open  = float(prev["open"])
        prev_close = float(prev["close"])
        prev_body  = abs(prev_open - prev_close)

        # ATR 대용: 최근 _ATR_PERIOD 봉의 high-low 평균
        lookback = min(len(ema_df), _ATR_PERIOD)
        recent_ranges = []
        for i in range(len(ema_df) - lookback, len(ema_df)):
            h = float(ema_df.iloc[i]["high"])
            l = float(ema_df.iloc[i]["low"])
            recent_ranges.append(h - l)
        avg_range = sum(recent_ranges) / len(recent_ranges) if recent_ranges else 0

        if avg_range > 0 and prev_body > avg_range * _PANIC_BODY_MULT:
            body_ratio = prev_body / avg_range
            meta["panic_body_ratio"] = round(body_ratio, 2)
            return BuySignal(ticker, False, current_price,
                             f"PANIC_CANDLE(body={body_ratio:.1f}x>ATR×{_PANIC_BODY_MULT})",
                             metadata=meta)

        # ── 조건 4: 이전 캔들 종가 < EMA10 (눌림목) ─────────────────────────
        if float(prev["close"]) >= ema10:
            return BuySignal(ticker, False, current_price, "NO_PULLBACK", metadata=meta)

        # ── 조건 5: 이전 캔들 저가 > EMA20 (EMA20 지지) ─────────────────────
        if float(prev["low"]) <= ema20:
            return BuySignal(ticker, False, current_price, "PULLBACK_BROKE_EMA20", metadata=meta)

        # ── 조건 6: 현재 캔들 종가 > EMA10 (재돌파) ─────────────────────────
        if float(cur["close"]) <= ema10:
            return BuySignal(ticker, False, current_price, "NOT_RECLAIMED_YET", metadata=meta)

        meta["stop_loss_pct"] = _SL_PCT
        logger.info(
            f"[triple_ema] ★ 눌림목돌파 | {ticker} | "
            f"EMA10={ema10:.0f} EMA20={ema20:.0f} EMA50={ema50:.0f} | "
            f"HTF_EMA{_HTF_EMA}={ema_htf:.0f} ADX={adx:.1f}"
        )
        return BuySignal(
            ticker=ticker, should_buy=True, current_price=current_price,
            reason="EMA_PULLBACK_RECLAIM+HTF+ADX", metadata=meta,
        )

    # ─── 매도 신호 (Trailing Stop) ────────────────────────────────────────────

    def should_sell_on_signal(self, ticker, current_price, position) -> SellSignal:
        entry = position.buy_price

        # 최고가 갱신
        peak = self._peaks.get(ticker, current_price)
        if current_price > peak:
            peak = current_price
        self._peaks[ticker] = peak

        # Trailing 활성화 기준: TP 50% 지점 (= entry × (1 + TP_PCT/2))
        activation_price = entry * (1 + _TP_PCT * 0.5)

        if peak < activation_price:
            # 미활성: risk_manager 하드 스탑에 위임
            return SellSignal(ticker, False, current_price, "")

        # 활성: trail_stop = max(본절가, peak × (1 − trail_pct))
        # trail_pct는 최소 TRAIL_MIN_PCT(1.5%) 보장
        trail_pct  = max(_SL_PCT, _TRAIL_MIN_PCT)
        trail_stop = max(entry, peak * (1 - trail_pct))

        if current_price <= trail_stop:
            pnl_pct = (current_price - entry) / entry * 100
            reason  = f"TRAIL_STOP({trail_stop:.0f}|peak={peak:.0f})"
            logger.info(
                f"[triple_ema] ★ Trailing Stop | {ticker} | "
                f"수익={pnl_pct:+.2f}% | {reason}"
            )
            self._peaks.pop(ticker, None)
            return SellSignal(ticker, True, current_price, reason)

        return SellSignal(ticker, False, current_price, "")
