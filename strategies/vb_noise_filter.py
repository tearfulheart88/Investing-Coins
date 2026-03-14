"""
전략: 변동성 돌파 + 노이즈 필터 + 이동평균선 필터 (개선판 v7)
시나리오 ID: vb_noise_filter

■ 개선 사항 (v7) — 2026-03-10
  - 파라미터 재조정: ADX_MIN 15→20, K_MIN 0.3→0.4, VOL_MULT 2.0→2.5, TIME_CUT 2.5→2.0h
  - 쿨다운 추가: 손절/타임컷 후 동일 종목 COOLDOWN_HOURS(2h) 재진입 금지
  - on_position_closed()에 reason 인자 추가 (쿨다운 조건 판단용)

■ 개선 사항 (v6) — 2026-03-08
  - EMA200(4h) 장기 추세 필터 추가: 현재가 < EMA200(4h) 시 하락 추세로 판단, 매수 제외
    → MR 전략과 동일한 장기 추세 기준 적용 (신규 상장 등 데이터 부족 시 우회)
    → EMA200 결과를 30분 캐시하여 API 호출 최소화
  - ADX(1h) 횡보 필터 추가: ADX < adx_min_vb(기본 15) 시 횡보 구간 → 가짜 돌파 위험 → 매수 제외
    → 데이터 부족 시 우회 (신규 상장 코인 보호)
  - 매수 필터 통계에 below_ema200, low_adx 항목 추가

■ 개선 사항 (v5) — 2026-03-07 Gemini 2차 분석 기반
  - 파라미터 재조정: vol_mult 2.5→2.0, trail_drop 0.5→1.0%,
    min_momentum 0.3→0.5%, time_cut 2.0→2.5h
  - ATR 적응 트레일링: ATR(14,1h)% 기반으로 trail_drop을 동적 설정
    → 변동성 높을수록 트레일링 폭 자동 확대 (노이즈 조기청산 방지)
    → 최소값 = _TRAIL_DROP_PCT (config/UI 설정값)
  - 매수 필터 통계: 필터별 거부 횟수 집계 로깅 (분석/디버깅용)
  - 매도 사유별 집계: TSL/BE/TC/SCHED 각각 카운트 + PnL 합계

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
  1. current_price >= EMA200(4h)                        (장기 상승 추세 확인)  ← v6 신규
  2. ADX(1h) >= adx_min_vb (기본 15)                   (최소 추세 강도 확인)  ← v6 신규
  3. current_price >= today_open + yesterday_range * k  (변동성 돌파)
  4. current_price > MA(15)                             (단기 상승 추세 확인)
  5. vol_cur_5m >= vol_sma_5m × VOL_MULT               (거래량 급증 확인)
  6. 당일 고가 이격도 >= 1%                              (윗꼬리 저항 회피)

매도 전략:
  TP  : 익일 09:00 KST 스케줄 매도 (requires_scheduled_sell=True)
  SL  : risk_manager 손절
  TSL : peak에서 -effective_trail% 하락 → 트레일링 청산  (우선순위 1)
        effective_trail = max(trail_drop_pct, ATR_1h% × atr_trail_mult)
  BE  : peak PnL ≥ BE_TRIGGER% → 현재가 ≤ 진입가+BE_FLOOR% → 본절 청산  (우선순위 2)
  TC  : 매수 후 TIME_CUT_HOURS 경과 + 수익률 < MIN_MOMENTUM% → 청산  (우선순위 3)
       (단, 1h봉 MACD+RSI 모멘텀 유지 시 1회 연장)

임포트 규칙:
  이 파일은 base_strategy, data.market_data 만 임포트.
  다른 전략 파일 임포트 금지.
"""
import logging
import time
from datetime import datetime, timezone, timedelta
from data.market_data import MarketData
from exchange.upbit_client import DataFetchError
from strategies.base_strategy import BaseStrategy, BuySignal, SellSignal
import config

logger = logging.getLogger(__name__)

# ─── 모듈 상수 (config.STRATEGY_PARAMS["vb"]에서 초기화, UI 슬라이더로 런타임 변경) ──
_vb = config.STRATEGY_PARAMS.get("vb", {})
_K_MIN              = _vb.get("k_min",            0.4)   # v7: 0.3→0.4 더 강한 돌파만
_K_MAX              = _vb.get("k_max",            0.8)
_TIME_CUT_HOURS     = _vb.get("time_cut_hours",   2.0)   # v7: 2.5→2.0 빠른 손절
_MIN_MOMENTUM_PCT   = _vb.get("min_momentum_pct", 0.5)
_VOL_MULT           = _vb.get("vol_mult",         2.5)   # v7: 2.0→2.5 더 강한 거래량 필터
_VOL_SMA_PERIOD     = 20
_BE_TRIGGER_PCT     = _vb.get("be_trigger_pct",   1.0)
_BE_FLOOR_PCT       = _vb.get("be_floor_pct",     0.2)
_TRAIL_DROP_PCT     = _vb.get("trail_drop_pct",   1.0)
_USE_ATR_TRAIL      = _vb.get("use_atr_trail",    True)
_ATR_TRAIL_MULT     = _vb.get("atr_trail_mult",   0.5)
_EMA200_FILTER      = _vb.get("ema200_filter",    True)   # v6: EMA200(4h) 장기 추세 필터
_ADX_MIN_VB         = _vb.get("adx_min_vb",       20.0)   # v7: 15→20 노이즈 진입 방지
_COOLDOWN_HOURS     = _vb.get("cooldown_hours",   2.0)    # v7: 손절 후 재진입 금지 시간
_HARD_SL_PCT        = _vb.get("hard_sl_pct",       3.0)   # v8: 인트라데이 하드 손절 (-N% 즉시 청산)
_BTC_MA20_FILTER    = _vb.get("btc_ma20_filter",  True)   # v8: BTC 1h MA20 거시 추세 필터
_CONFIRM_CANDLES    = _vb.get("confirm_candles",  True)   # v8: 5분봉 2개 연속 양봉 확인 필터
_BTC_MA20_CACHE_SEC = 300.0  # BTC MA20 캐시 유효시간 (5분)

_KST = timezone(timedelta(hours=9))
_ATR_CACHE_SEC   = 300.0    # ATR 캐시 유효시간 (5분)
_EMA200_CACHE_SEC = 1800.0  # EMA200(4h) 캐시 유효시간 (30분, 4h봉 기준 변동 느림)


class VBNoiseFilterStrategy(BaseStrategy):

    def __init__(self, market_data: MarketData) -> None:
        self._md = market_data
        self._peaks: dict[str, float] = {}           # ticker → 최고가 (본절방어 + 트레일링)
        self._time_cut_extended: set[str] = set()     # 타임컷 1회 연장 사용 추적

        # v5: ATR 기반 동적 트레일링 캐시 — ticker → (effective_trail_pct, timestamp)
        self._atr_trail_cache: dict[str, tuple[float, float]] = {}
        # v6: EMA200(4h) 캐시 — ticker → (ema200_value, timestamp)
        self._ema200_cache: dict[str, tuple[float, float]] = {}
        # v7: 손절 후 쿨다운 — ticker → cooldown_end_time (time.time 기준)
        self._cooldowns: dict[str, float] = {}
        # v8: BTC 1h MA20 캐시 — (btc_ma20, btc_price, timestamp)
        self._btc_ma20_cache: tuple[float, float, float] | None = None

        # v5: 매수 필터 통계 (분석/디버깅용)
        self._buy_stats: dict[str, int] = {
            "total":          0,   # should_buy 호출 총 횟수
            "cooldown":       0,   # v7: 손절 후 쿨다운 중
            "below_ema200":   0,   # v6: EMA200(4h) 하락 추세 제외
            "btc_bearish":    0,   # v8: BTC 1h MA20 하락 추세 차단
            "low_adx":        0,   # v6: ADX 낮음 (횡보 필터)
            "no_breakout":    0,   # 변동성 돌파 미달
            "no_uptrend":     0,   # MA 상승추세 미충족
            "no_volume":      0,   # 거래량 급증 미달
            "false_breakout": 0,   # v8: 5분봉 연속 양봉 미확인 (거짓 돌파)
            "high_proximity": 0,   # 윗꼬리 저항 근접
            "sell_pressure":  0,   # v9: 호가창 매도 압력 우위 (gabagool22 인사이트)
            "data_error":     0,   # 데이터 조회 오류
            "passed":         0,   # 모든 필터 통과 (매수 신호)
        }
        # v5: 매도 사유별 통계
        self._sell_stats: dict[str, dict] = {
            "HARD_SL": {"count": 0, "total_pnl": 0.0},  # v8: 인트라데이 하드 손절
            "TSL":     {"count": 0, "total_pnl": 0.0},
            "BE":      {"count": 0, "total_pnl": 0.0},
            "TC":      {"count": 0, "total_pnl": 0.0},
        }

    def get_strategy_id(self) -> str:
        return "volatility_breakout"

    def get_scenario_id(self) -> str:
        return "vb_noise_filter"

    def requires_scheduled_sell(self) -> bool:
        return True

    def get_history_requirements(self) -> dict[str, int]:
        req = {
            "day": max(config.MA_PERIOD, config.NOISE_FILTER_DAYS + 1, 3),
            "minute5": _VOL_SMA_PERIOD + 1,
        }
        if _ADX_MIN_VB > 0 or _USE_ATR_TRAIL:
            req["minute60"] = 60
        if _EMA200_FILTER:
            req["minute240"] = 195
        return req

    def get_ticker_selection_profile(self) -> dict:
        return {
            "pattern": "vol_breakout_filtered",
            "pool_size": 80,
            "refresh_hours": 0.5,
        }

    # ─── 포지션 종료 시 내부 상태 정리 (스케줄매도/외부 매도 공통) ────────────
    def on_position_closed(self, ticker: str, reason: str = "") -> None:
        """
        포지션 종료 시 호출. 스케줄 매도(09:00), 손절, 수동 청산 등
        어떤 경로로든 포지션이 종료되면 내부 추적 상태를 정리한다.
        v7: 손절(ENGINE_STOP/HARD_SL/TIME_CUT) 시 쿨다운 등록.
        """
        self._peaks.pop(ticker, None)
        self._time_cut_extended.discard(ticker)
        self._atr_trail_cache.pop(ticker, None)
        self._ema200_cache.pop(ticker, None)

        # v7: 손절 or 타임컷 청산 시 쿨다운 등록
        if reason and any(tag in reason for tag in ("STOP_LOSS", "ENGINE_STOP", "HARD_SL", "TIME_CUT")):
            cd_end = time.time() + _COOLDOWN_HOURS * 3600
            self._cooldowns[ticker] = cd_end
            logger.info(
                f"[vb_noise_filter] 쿨다운 등록 | {ticker} | "
                f"{_COOLDOWN_HOURS}h 재진입 금지 | reason={reason}"
            )

    def on_position_reentered(
        self, ticker: str, new_entry_price: float, reason: str = ""
    ) -> None:
        """
        재진입 시 내부 추적 상태 리셋.
        - _peaks: 현재가(새 매수가)로 리셋 → 트레일링이 새 기준에서 시작
        - _time_cut_extended: 해제 → 새 TIME_CUT 윈도우 확보
        """
        self._peaks[ticker] = new_entry_price
        self._time_cut_extended.discard(ticker)
        logger.info(
            f"[vb_noise_filter] 재진입 상태 리셋 | {ticker} | "
            f"peak={new_entry_price:,.0f} | time_cut_extended=cleared"
        )


    def reset_daily(self) -> None:
        """09:00 스케줄 매도 후 일괄 초기화."""
        # 일별 통계 로깅 (0건이 아닌 경우에만)
        if self._buy_stats["total"] > 0:
            logger.info(
                f"[vb_noise_filter] 일별 매수필터 통계 | "
                f"총={self._buy_stats['total']} "
                f"CD={self._buy_stats['cooldown']} "
                f"EMA200X={self._buy_stats['below_ema200']} "
                f"BTCX={self._buy_stats['btc_bearish']} "
                f"ADXX={self._buy_stats['low_adx']} "
                f"돌파X={self._buy_stats['no_breakout']} "
                f"추세X={self._buy_stats['no_uptrend']} "
                f"거래량X={self._buy_stats['no_volume']} "
                f"거짓돌파={self._buy_stats['false_breakout']} "
                f"고점근접={self._buy_stats['high_proximity']} "
                f"에러={self._buy_stats['data_error']} "
                f"통과={self._buy_stats['passed']}"
            )
        if any(v["count"] > 0 for v in self._sell_stats.values()):
            parts = []
            for key, val in self._sell_stats.items():
                if val["count"] > 0:
                    avg = val["total_pnl"] / val["count"]
                    parts.append(f"{key}={val['count']}건(avg={avg:+.2f}%)")
            logger.info(f"[vb_noise_filter] 일별 매도사유 통계 | {' '.join(parts)}")

        self._peaks.clear()
        self._time_cut_extended.clear()
        self._atr_trail_cache.clear()
        self._ema200_cache.clear()
        # v7: 쿨다운은 일별 초기화하지 않음 (시간 기반으로 자동 만료)
        # 통계 초기화
        for k in self._buy_stats:
            self._buy_stats[k] = 0
        for v in self._sell_stats.values():
            v["count"] = 0
            v["total_pnl"] = 0.0

    # ─── ATR 기반 동적 트레일링 폭 계산 ────────────────────────────────────

    def _get_effective_trail_drop(self, ticker: str) -> float:
        """
        ATR(14, 1h) 기반으로 트레일링 폭을 동적으로 결정.
        effective_trail = max(_TRAIL_DROP_PCT, ATR_1h_pct * 100 * _ATR_TRAIL_MULT)

        ATR 기능 비활성 또는 조회 실패 시 → _TRAIL_DROP_PCT 고정값 반환.
        결과를 5분간 캐시하여 API 호출 최소화.
        """
        if not _USE_ATR_TRAIL:
            return _TRAIL_DROP_PCT

        now = time.time()
        cached = self._atr_trail_cache.get(ticker)
        if cached:
            val, ts = cached
            if now - ts < _ATR_CACHE_SEC:
                return val

        try:
            _, atr_pct, _ = self._md.compute_atr_pct(ticker, period=14, interval="minute60")
            atr_trail = atr_pct * 100 * _ATR_TRAIL_MULT
            effective = max(_TRAIL_DROP_PCT, atr_trail)
            # 상한 제한: 너무 넓은 trailing은 수익 보호 실패
            effective = min(effective, 3.0)
        except Exception:
            effective = _TRAIL_DROP_PCT

        self._atr_trail_cache[ticker] = (effective, now)
        return effective

    # ─── 매수 ────────────────────────────────────────────────────────────────

    def should_buy(self, ticker: str, current_price: float) -> BuySignal:
        self._buy_stats["total"] += 1

        # ── [v7] 손절 후 쿨다운 체크 ──────────────────────────────────────────
        cd_end = self._cooldowns.get(ticker, 0.0)
        if cd_end > 0:
            now = time.time()
            if now < cd_end:
                remaining_min = (cd_end - now) / 60
                self._buy_stats["cooldown"] += 1
                return BuySignal(
                    ticker=ticker, should_buy=False,
                    current_price=current_price,
                    reason=f"COOLDOWN({remaining_min:.0f}min)",
                )
            else:
                # 쿨다운 만료 → 정리
                del self._cooldowns[ticker]

        try:
            k_raw            = self._md.compute_noise_filter_k(ticker, days=config.NOISE_FILTER_DAYS)
            k                = max(_K_MIN, min(_K_MAX, k_raw))
            target_price     = self._md.compute_target_price(ticker, k)
            ma               = self._md.compute_ma(ticker, period=config.MA_PERIOD)
            vol_cur, vol_sma = self._md.compute_volume_sma_intraday(ticker, _VOL_SMA_PERIOD, "minute5")
            df_daily         = self._md.get_ohlcv(ticker, count=3)
        except DataFetchError as e:
            self._buy_stats["data_error"] += 1
            logger.warning(f"[vb_noise_filter] 데이터 조회 실패, 매수 건너뜀 | {ticker}: {e}")
            return BuySignal(
                ticker=ticker,
                should_buy=False,
                current_price=current_price,
                reason="DATA_ERROR",
            )

        # ── [v6] EMA200(4h) 장기 추세 필터 ──────────────────────────────────
        # 현재가 < EMA200(4h) → 장기 하락 추세 → 매수 제외
        # 데이터 부족(신규 상장 등) 시 필터 우회 (30분 캐시)
        ema200_4h: float | None = None
        if _EMA200_FILTER:
            now_ts = time.time()
            cached_ema = self._ema200_cache.get(ticker)
            if cached_ema and now_ts - cached_ema[1] < _EMA200_CACHE_SEC:
                ema200_4h = cached_ema[0]
            else:
                try:
                    ema200_4h = self._md.compute_ema_intraday(ticker, 200, "minute240")
                    self._ema200_cache[ticker] = (ema200_4h, now_ts)
                except Exception:
                    pass  # 데이터 부족 → 필터 우회

        if ema200_4h is not None and current_price < ema200_4h:
            self._buy_stats["below_ema200"] += 1
            reason = f"BELOW_EMA200_4H({current_price:,.0f}<{ema200_4h:,.0f})"
            logger.debug(f"[vb_noise_filter] {ticker} EMA200(4h) 하락 추세 제외 | {reason}")
            return BuySignal(
                ticker=ticker, should_buy=False,
                current_price=current_price, reason=reason,
            )

        # ── [v8] BTC 1h MA20 거시 추세 필터 ──────────────────────────────
        # BTC가 1h MA20 아래에 있으면 전반적 하락장 → 모든 종목 매수 차단
        # 5분 캐시로 API 호출 최소화 (종목별로 매번 호출하지 않음)
        if _BTC_MA20_FILTER and ticker != "KRW-BTC":
            try:
                now_ts = time.time()
                if (self._btc_ma20_cache is None
                        or now_ts - self._btc_ma20_cache[2] > _BTC_MA20_CACHE_SEC):
                    btc_1h = self._md.get_ohlcv_intraday(
                        "KRW-BTC", interval="minute60", count=22
                    )
                    if btc_1h is not None and len(btc_1h) >= 21:
                        _bp = float(btc_1h.iloc[-1]["close"])
                        _bm = float(btc_1h["close"].iloc[-20:].mean())
                        self._btc_ma20_cache = (_bm, _bp, now_ts)
                if self._btc_ma20_cache:
                    btc_ma20_v, btc_price_v, _ = self._btc_ma20_cache
                    if btc_price_v < btc_ma20_v:
                        self._buy_stats["btc_bearish"] += 1
                        return BuySignal(
                            ticker=ticker, should_buy=False,
                            current_price=current_price,
                            reason=f"BTC_BEARISH(btc={btc_price_v:,.0f}<ma20={btc_ma20_v:,.0f})",
                        )
            except Exception:
                pass  # BTC 데이터 조회 실패 시 필터 우회

        # ── [v6] ADX(1h) 추세 강도 확인 (데이터 부족 시 우회) ──────────────
        # ADX < adx_min_vb → 횡보 구간 → 가짜 돌파 위험 → 매수 제외
        adx_1h: float | None = None
        if _ADX_MIN_VB > 0:
            try:
                adx_1h = self._md.compute_adx(ticker, 14, "minute60")
            except Exception:
                pass  # 데이터 부족 → 필터 우회

        # ── 변동성 돌파 + MA 단기 추세 + 거래량 판정 ─────────────────────────
        breakout  = current_price >= target_price
        uptrend   = current_price > ma
        vol_ratio = vol_cur / vol_sma if vol_sma > 0 else 0.0
        vol_ok    = vol_ratio >= _VOL_MULT

        if breakout and uptrend and vol_ok:
            reason = "BREAKOUT+MA_FILTER+VOL"
            should = True
        elif breakout and uptrend and not vol_ok:
            self._buy_stats["no_volume"] += 1
            reason = f"BREAKOUT+MA_NO_VOL({vol_ratio:.2f}x<{_VOL_MULT}x)"
            should = False
        elif breakout and not uptrend:
            self._buy_stats["no_uptrend"] += 1
            reason = "BREAKOUT_NO_UPTREND"
            should = False
        else:
            self._buy_stats["no_breakout"] += 1
            reason = "NO_BREAKOUT"
            should = False

        # ── ADX 횡보 필터: 너무 낮으면 가짜 돌파 위험 ───────────────────────
        if should and adx_1h is not None and adx_1h < _ADX_MIN_VB:
            self._buy_stats["low_adx"] += 1
            reason = f"ADX_TOO_LOW({adx_1h:.1f}<{_ADX_MIN_VB:.0f})"
            should = False

        # ── [v8] 거짓 돌파 확인: 5분봉 직전 2개가 모두 양봉이어야 진입 ──────
        # 돌파 신호가 나도 봉이 아직 음봉이면 세력의 유인 가능성 → 대기
        if should and _CONFIRM_CANDLES:
            try:
                df_5m_c = self._md.get_ohlcv_intraday(ticker, interval="minute5", count=4)
                if df_5m_c is not None and len(df_5m_c) >= 2:
                    c1_bull = float(df_5m_c.iloc[-1]["close"]) > float(df_5m_c.iloc[-1]["open"])
                    c2_bull = float(df_5m_c.iloc[-2]["close"]) > float(df_5m_c.iloc[-2]["open"])
                    if not (c1_bull and c2_bull):
                        self._buy_stats["false_breakout"] += 1
                        reason = (
                            f"FALSE_BREAKOUT("
                            f"c1={'bull' if c1_bull else 'bear'}"
                            f"|c2={'bull' if c2_bull else 'bear'})"
                        )
                        should = False
            except Exception:
                pass  # 데이터 조회 실패 시 필터 우회

        # ── [v9] 호가창 압력 필터: 매도 압력 우위 시 진입 차단 ─────────────────
        # gabagool22 마켓메이킹 인사이트: 실수요 없이 기술 신호만으로 진입하면 즉시 역행
        if should and self._orderbook_cache is not None:
            try:
                ob = self._orderbook_cache.get(ticker)
                if ob is not None and ob.total_bid_size > 0 and ob.total_ask_size > 0:
                    if ob.total_ask_size > ob.total_bid_size * 1.3:
                        self._buy_stats["sell_pressure"] += 1
                        reason = (
                            f"SELL_PRESSURE("
                            f"ask={ob.total_ask_size:.2f}"
                            f">bid={ob.total_bid_size:.2f}x1.3)"
                        )
                        should = False
            except Exception:
                pass  # 호가 데이터 없으면 필터 우회 (fail-safe)

        # ── 고점 이격도 필터: 당일 고가 아래 1% 이내 → 윗꼬리 저항 회피 ────
        if should and not df_daily.empty:
            try:
                today_high = float(df_daily.iloc[-1]["high"])
                if today_high > 0 and current_price < today_high:
                    proximity_pct = (today_high - current_price) / today_high * 100
                    if proximity_pct < 1.0:
                        self._buy_stats["high_proximity"] += 1
                        should = False
                        reason = (
                            f"HIGH_PROXIMITY({proximity_pct:.2f}%<1%"
                            f"|high={today_high:,.0f})"
                        )
            except Exception as e:
                logger.debug(f"[vb_noise_filter] 고점 이격도 계산 오류: {e}")

        if should:
            self._buy_stats["passed"] += 1

        ema_str = f"{ema200_4h:,.0f}" if ema200_4h is not None else "N/A"
        adx_str = f"{adx_1h:.1f}" if adx_1h is not None else "N/A"
        logger.debug(
            f"[vb_noise_filter] {ticker} | price={current_price:,.0f} "
            f"target={target_price:,.0f} ma={ma:,.0f} "
            f"k={k:.3f}(raw={k_raw:.3f}) vol={vol_ratio:.2f}x "
            f"ema200_4h={ema_str} adx_1h={adx_str} → {reason}"
        )

        # 주기적 통계 출력 (50회마다)
        if self._buy_stats["total"] % 50 == 0:
            logger.info(
                f"[vb_noise_filter] 매수필터 중간집계 | "
                f"총={self._buy_stats['total']} "
                f"통과={self._buy_stats['passed']} "
                f"CD={self._buy_stats['cooldown']} "
                f"EMA200X={self._buy_stats['below_ema200']} "
                f"BTCX={self._buy_stats['btc_bearish']} "
                f"ADXX={self._buy_stats['low_adx']} "
                f"돌파X={self._buy_stats['no_breakout']} "
                f"추세X={self._buy_stats['no_uptrend']} "
                f"거래량X={self._buy_stats['no_volume']} "
                f"거짓돌파={self._buy_stats['false_breakout']} "
                f"고점={self._buy_stats['high_proximity']} "
                f"매도압력={self._buy_stats['sell_pressure']}"
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
                "ema200_4h": round(ema200_4h, 0) if ema200_4h is not None else None,
                "adx_1h": round(adx_1h, 1) if adx_1h is not None else None,
                "tp_label": "09:00/동적",
            },
        )

    # ─── 매도 ────────────────────────────────────────────────────────────────

    def should_sell_on_signal(
        self, ticker: str, current_price: float, position
    ) -> SellSignal:
        entry = position.buy_price
        pnl_pct = (current_price - entry) / entry * 100

        # ── [0순위] 하드 손절: 진입가 대비 -N% 즉시 청산 (v8) ───────────────
        # TIME_CUT은 2h 후 발동이라 그 전까지 -3%+ 손실을 방치하는 문제 방지
        # BSV(-3.03%, 99분 보유), SAHARA(-3.29%, 39분) 같은 케이스 재발 차단
        if pnl_pct <= -_HARD_SL_PCT:
            reason = f"HARD_SL(pnl={pnl_pct:+.2f}%<=-{_HARD_SL_PCT:.1f}%)"
            logger.info(
                f"[vb_noise_filter] ★ 하드 손절 | {ticker} | "
                f"entry={entry:,.0f} now={current_price:,.0f} | {reason}"
            )
            self._sell_stats["HARD_SL"]["count"] += 1
            self._sell_stats["HARD_SL"]["total_pnl"] += pnl_pct
            self.on_position_closed(ticker, reason)
            return SellSignal(ticker, True, current_price, reason)

        # ── 최고가 갱신 (본절 방어 + 트레일링용) ─────────────────────────────
        peak = self._peaks.get(ticker, current_price)
        if current_price > peak:
            peak = current_price
        self._peaks[ticker] = peak

        peak_pnl_pct = (peak - entry) / entry * 100

        # ── ATR 적응 트레일링 폭 (v5) ─────────────────────────────────────
        effective_trail = self._get_effective_trail_drop(ticker)

        # ── [1순위] 트레일링: peak에서 effective_trail 이상 하락 시 청산 ──────
        if peak_pnl_pct >= _BE_TRIGGER_PCT:
            drop_from_peak = (peak - current_price) / peak * 100
            if drop_from_peak >= effective_trail:
                reason = (
                    f"TRAIL_DROP(peak={peak_pnl_pct:+.2f}%"
                    f"|drop={drop_from_peak:.2f}%>={effective_trail:.2f}%"
                    f"|pnl={pnl_pct:+.2f}%)"
                )
                logger.info(
                    f"[vb_noise_filter] ★ 트레일링 청산 | {ticker} | "
                    f"entry={entry:,.0f} peak={peak:,.0f} now={current_price:,.0f} "
                    f"trail={effective_trail:.2f}% | {reason}"
                )
                self._sell_stats["TSL"]["count"] += 1
                self._sell_stats["TSL"]["total_pnl"] += pnl_pct
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
                self._sell_stats["BE"]["count"] += 1
                self._sell_stats["BE"]["total_pnl"] += pnl_pct
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
                            f"[vb_noise_filter] 타임컷 연장 | {ticker} | "
                            f"pnl={pnl_pct:+.2f}% elapsed={elapsed_hours:.1f}h | "
                            f"MACD_1H={macd_hist_val:.4f} RSI_1H={rsi_val:.1f} "
                            f"-> 모멘텀 유지, 1회 바이패스"
                        )
                        # 연장 — 이번 사이클 보유 유지
                    else:
                        reason = (
                            f"TIME_CUT({elapsed_hours:.1f}h"
                            f"|pnl={pnl_pct:+.2f}%<{_MIN_MOMENTUM_PCT}%"
                            f"|MACD={macd_hist_val:.4f}|RSI={rsi_val:.1f})"
                        )
                        logger.info(
                            f"[vb_noise_filter] 타임컷 청산 | {ticker} | "
                            f"entry={entry:,.0f} now={current_price:,.0f} | {reason}"
                        )
                        self._sell_stats["TC"]["count"] += 1
                        self._sell_stats["TC"]["total_pnl"] += pnl_pct
                        self.on_position_closed(ticker)
                        return SellSignal(ticker, True, current_price, reason)
                else:
                    # 이미 1회 연장 사용 → 무조건 최종 청산 (무한 연장 방지)
                    reason = (
                        f"TIME_CUT_FINAL({elapsed_hours:.1f}h"
                        f"|pnl={pnl_pct:+.2f}%<{_MIN_MOMENTUM_PCT}%"
                        f"|ext_used)"
                    )
                    logger.info(
                        f"[vb_noise_filter] 타임컷 최종청산 | {ticker} | "
                        f"entry={entry:,.0f} now={current_price:,.0f} | {reason}"
                    )
                    self._sell_stats["TC"]["count"] += 1
                    self._sell_stats["TC"]["total_pnl"] += pnl_pct
                    self.on_position_closed(ticker)
                    return SellSignal(ticker, True, current_price, reason)
        except Exception as e:
            logger.warning(f"[vb_noise_filter] time-cut 계산 오류: {e}")

        # VB 전략은 기본적으로 스케줄(09:00) 또는 손절로만 매도
        return SellSignal(ticker, False, current_price, "")
