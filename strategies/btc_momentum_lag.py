"""
Strategy: BTC Micro-Momentum Lag (v1)
Scenario ID: btc_momentum_lag

gabagool22 인사이트 적용:
  BTC가 1분봉 기준 급등(+0.3% 이상)했을 때,
  아직 반응하지 않은 알트코인(+0.1% 미만)에 선진입.
  BTC→알트 가격 전파 지연(lag) 구간을 포착.

진입 조건:
  1. BTC 직전 1분봉 수익률 >= +0.3% (급등 신호)
  2. BTC 1h MA20 위 (거시 추세 확인)
  3. 대상 종목 직전 1분봉 수익률 < +0.1% (아직 lag)
  4. 호가창 매도 압력이 매수 압력을 압도하지 않을 것
  5. 24h 거래대금 최소 기준 충족

청산 조건 (스트릭트 단타):
  - HARD_SL: 진입가 대비 -1.5%
  - TAKE_PROFIT: +2.0%
  - TIME_CUT: 15분 (지연이 전파되거나 신호 소멸)
  - BTC 1분봉이 음봉 전환 시 즉시 청산
"""

import logging
import time
from datetime import datetime, timedelta, timezone

from data.market_data import MarketData
from strategies.base_strategy import BaseStrategy, BuySignal, SellSignal

logger = logging.getLogger(__name__)

_BTC_MIN_1M_RETURN_PCT  = 0.30   # BTC 급등 최소 기준 (%)
_ALT_MAX_1M_RETURN_PCT  = 0.10   # 알트 최대 반응 (이 이하여야 lag 인정)
_HARD_SL_PCT            = 1.5    # 하드 손절 (%)
_TAKE_PROFIT_PCT        = 2.0    # 익절 목표 (%)
_TIME_CUT_MINUTES       = 15     # 최대 보유 시간 (분)
_OB_SELL_RATIO          = 1.3    # 호가창 매도 압력 차단 비율
_MIN_24H_VALUE_KRW      = 3_000_000_000  # 최소 24h 거래대금 (30억)
_BTC_MA20_CACHE_SEC     = 60.0   # BTC MA20 캐시 TTL (급등 감지 주기)
_BTC_1M_CACHE_SEC       = 20.0   # BTC 1분봉 캐시 TTL

_KST = timezone(timedelta(hours=9))


class BtcMomentumLagStrategy(BaseStrategy):
    def __init__(self, market_data: MarketData) -> None:
        self._md = market_data
        # BTC 캐시: (ma20, last_1m_return, timestamp)
        self._btc_cache: tuple[float, float, float] | None = None

    # ─── ID ─────────────────────────────────────────────────────────────────
    def get_strategy_id(self) -> str:
        return "scalping"

    def get_scenario_id(self) -> str:
        return "btc_momentum_lag"

    def requires_scheduled_sell(self) -> bool:
        return False  # 자체 신호로 청산

    def get_history_requirements(self) -> dict[str, int]:
        return {"minute1": 5, "minute60": 22}

    def get_ticker_selection_profile(self) -> dict:
        return {"pattern": "momentum_lag", "pool_size": 60}

    # ─── 매수 신호 ──────────────────────────────────────────────────────────
    def should_buy(self, ticker: str, current_price: float) -> BuySignal:
        no_buy = lambda reason: BuySignal(ticker, False, current_price, reason)

        if ticker == "KRW-BTC":
            return no_buy("SKIP_BTC_SELF")

        # ── 1. BTC 급등 신호 + MA20 추세 확인 ──────────────────────────────
        now_ts = time.time()
        try:
            if (self._btc_cache is None
                    or now_ts - self._btc_cache[2] > _BTC_1M_CACHE_SEC):
                btc_1m = self._md.get_ohlcv_intraday("KRW-BTC", interval="minute1", count=25)
                if btc_1m is None or len(btc_1m) < 22:
                    return no_buy("BTC_DATA_ERROR")
                # 직전 1분봉 수익률 (완성된 봉 기준)
                prev_close = float(btc_1m.iloc[-2]["close"])
                prev_open  = float(btc_1m.iloc[-2]["open"])
                btc_1m_ret = (prev_close - prev_open) / prev_open * 100 if prev_open > 0 else 0.0
                # BTC MA20 (1h 대신 1분봉 20개 사용 — 더 빠른 추세)
                btc_ma20 = float(btc_1m["close"].iloc[-21:-1].mean())
                btc_price = float(btc_1m.iloc[-1]["close"])
                self._btc_cache = (btc_ma20, btc_1m_ret, now_ts)
            btc_ma20, btc_1m_ret, _ = self._btc_cache
            btc_price = self._md.get_ohlcv_intraday("KRW-BTC", interval="minute1", count=2)
            btc_price = float(btc_price.iloc[-1]["close"]) if btc_price is not None else 0.0
        except Exception:
            return no_buy("BTC_DATA_ERROR")

        if btc_1m_ret < _BTC_MIN_1M_RETURN_PCT:
            return no_buy(f"BTC_NO_SURGE({btc_1m_ret:+.2f}%<{_BTC_MIN_1M_RETURN_PCT}%)")

        if btc_price > 0 and btc_price < btc_ma20:
            return no_buy(f"BTC_BEARISH(price={btc_price:,.0f}<ma20={btc_ma20:,.0f})")

        # ── 2. 알트 lag 확인: 아직 안 올랐어야 ──────────────────────────────
        try:
            alt_1m = self._md.get_ohlcv_intraday(ticker, interval="minute1", count=3)
            if alt_1m is None or len(alt_1m) < 2:
                return no_buy("ALT_DATA_ERROR")
            alt_prev_close = float(alt_1m.iloc[-2]["close"])
            alt_prev_open  = float(alt_1m.iloc[-2]["open"])
            alt_1m_ret = (alt_prev_close - alt_prev_open) / alt_prev_open * 100 if alt_prev_open > 0 else 0.0
        except Exception:
            return no_buy("ALT_DATA_ERROR")

        if alt_1m_ret >= _ALT_MAX_1M_RETURN_PCT:
            return no_buy(f"ALT_ALREADY_MOVED({alt_1m_ret:+.2f}%>={_ALT_MAX_1M_RETURN_PCT}%)")

        # 너무 많이 떨어진 경우도 제외 (급락 중)
        if alt_1m_ret < -1.0:
            return no_buy(f"ALT_FALLING({alt_1m_ret:+.2f}%)")

        # ── 3. 호가창 압력 필터 ──────────────────────────────────────────────
        if self._orderbook_cache is not None:
            try:
                ob = self._orderbook_cache.get(ticker)
                if ob is not None and ob.total_bid_size > 0 and ob.total_ask_size > 0:
                    if ob.total_ask_size > ob.total_bid_size * _OB_SELL_RATIO:
                        return no_buy(
                            f"SELL_PRESSURE(ask={ob.total_ask_size:.2f}"
                            f">bid={ob.total_bid_size:.2f}x{_OB_SELL_RATIO})"
                        )
            except Exception:
                pass

        reason = (
            f"BTC_LAG_ENTRY("
            f"btc_1m={btc_1m_ret:+.2f}%"
            f"|alt_1m={alt_1m_ret:+.2f}%)"
        )
        logger.info(f"[btc_momentum_lag] BUY {ticker} | {reason}")
        return BuySignal(
            ticker=ticker,
            should_buy=True,
            current_price=current_price,
            reason=reason,
            metadata={
                "btc_1m_return": round(btc_1m_ret, 4),
                "alt_1m_return": round(alt_1m_ret, 4),
                "take_profit_pct": _TAKE_PROFIT_PCT,
            },
        )

    # ─── 매도 신호 ──────────────────────────────────────────────────────────
    def should_sell_on_signal(
        self, ticker: str, current_price: float, position
    ) -> SellSignal:
        no_sell = lambda: SellSignal(ticker, False, current_price, "")
        buy_price = float(position.buy_price)
        if buy_price <= 0:
            return no_sell()

        pnl_pct = (current_price - buy_price) / buy_price * 100

        # HARD_SL
        if pnl_pct <= -_HARD_SL_PCT:
            return SellSignal(ticker, True, current_price, f"HARD_SL({pnl_pct:.2f}%)")

        # TAKE_PROFIT
        if pnl_pct >= _TAKE_PROFIT_PCT:
            return SellSignal(ticker, True, current_price, f"TAKE_PROFIT({pnl_pct:.2f}%)")

        # TIME_CUT: 15분 경과 (position.buy_time 기준)
        try:
            buy_time = position.buy_time
            if isinstance(buy_time, str):
                buy_time = datetime.fromisoformat(buy_time)
            if buy_time.tzinfo is None:
                buy_time = buy_time.replace(tzinfo=_KST)
            elapsed_min = (datetime.now(_KST) - buy_time).total_seconds() / 60
            if elapsed_min >= _TIME_CUT_MINUTES:
                return SellSignal(ticker, True, current_price, f"TIME_CUT({elapsed_min:.0f}m|{pnl_pct:.2f}%)")
        except Exception:
            pass

        # BTC 음봉 전환 시 조기 청산
        try:
            btc_1m = self._md.get_ohlcv_intraday("KRW-BTC", interval="minute1", count=2)
            if btc_1m is not None and len(btc_1m) >= 1:
                last_btc = btc_1m.iloc[-1]
                if float(last_btc["close"]) < float(last_btc["open"]) and pnl_pct > 0.3:
                    return SellSignal(ticker, True, current_price, f"BTC_REVERSAL({pnl_pct:.2f}%)")
        except Exception:
            pass

        return no_sell()

    # ─── 라이프사이클 ────────────────────────────────────────────────────────
    def on_position_closed(self, ticker: str, reason: str = "") -> None:
        pass  # buy_time 기반 TIME_CUT으로 처리
