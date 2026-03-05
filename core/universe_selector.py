"""
스코어 기반 종목 선정 — UniverseSelector

거래대금(50%) + 변동성(30%) − 스프레드(20%) 가중 점수로
업비트 KRW 마켓에서 최적의 거래 종목을 선정.

■ 스코어 공식:
  score = 0.50 × log(acc_trade_price_24h)
        + 0.30 × volatility_24h
        − 0.20 × spread_bps

■ 하드 필터 (강제 제외):
  - 24h 거래대금 < min_24h_value_krw (기본 50억)
  - spread_bps > max_spread_bps (기본 25)
  - 스테이블코인 블랙리스트

■ 사용법:
  selector = UniverseSelector(orderbook_cache, market_data)
  tickers = selector.select_top_n(n=10)
"""

import math
import logging
import time
import requests

from data.market_data import MarketData
from exchange.orderbook_manager import OrderbookCache
from exchange.upbit_client import DataFetchError

logger = logging.getLogger(__name__)

# 스테이블코인 블랙리스트
_BLACKLIST: frozenset[str] = frozenset({
    "KRW-USDT", "KRW-USDC", "KRW-DAI", "KRW-BUSD",
})

# 스코어 가중치
_W_VALUE:  float = 0.50    # 거래대금 비중
_W_VOL:    float = 0.30    # 변동성 비중
_W_SPREAD: float = -0.20   # 스프레드 비중 (음수: 낮을수록 좋음)


class UniverseSelector:
    """
    업비트 KRW 마켓에서 스코어 기반으로 상위 N개 종목 선정.

    Parameters
    ----------
    orderbook_cache : OrderbookCache | None
        실시간 호가 캐시. None이면 스프레드 없이 거래대금만 사용 (기존 방식 폴백).
    market_data : MarketData | None
        ATR 계산용. None이면 변동성 점수 제외.
    min_24h_value_krw : float
        최소 24h 거래대금 (KRW). 기본 5,000,000,000 (50억).
    max_spread_bps : float
        최대 스프레드 (bps). 기본 25.
    additional_blacklist : list[str] | None
        추가 제외 종목.
    """

    def __init__(
        self,
        orderbook_cache: OrderbookCache | None = None,
        market_data: MarketData | None = None,
        min_24h_value_krw: float = 5_000_000_000,
        max_spread_bps: float = 25.0,
        additional_blacklist: list[str] | None = None,
    ) -> None:
        self._ob_cache = orderbook_cache
        self._md = market_data
        self._min_24h_value = min_24h_value_krw
        self._max_spread_bps = max_spread_bps
        self._blacklist = _BLACKLIST | frozenset(additional_blacklist or [])

    def select_top_n(self, n: int = 10) -> list[str]:
        """
        상위 N개 종목 선정.

        1. 업비트 전체 KRW 종목 시세 조회 (24h 거래대금)
        2. 하드 필터 적용 (거래대금, 블랙리스트)
        3. 스프레드 필터 적용 (orderbook_cache 있을 때)
        4. 변동성(ATR%) 계산 (market_data 있을 때)
        5. 스코어 계산 + 정렬 → 상위 N개 반환
        """
        # 1. 시세 조회
        try:
            ticker_data = self._fetch_ticker_data()
        except Exception as e:
            logger.error(f"[UniverseSelector] 시세 조회 실패: {e}")
            # 폴백: 기존 단순 방식
            return MarketData.get_top_tickers_by_volume(n)

        # 2. 하드 필터
        candidates = []
        for d in ticker_data:
            market = d.get("market", "")
            if market in self._blacklist:
                continue
            if not market.startswith("KRW-"):
                continue

            value_24h = float(d.get("acc_trade_price_24h") or 0)
            if value_24h < self._min_24h_value:
                continue

            candidates.append({
                "ticker": market,
                "value_24h": value_24h,
                "high": float(d.get("high_price") or 0),
                "low": float(d.get("low_price") or 0),
                "close": float(d.get("trade_price") or 0),
            })

        if not candidates:
            logger.warning("[UniverseSelector] 필터 통과 종목 없음 → 폴백")
            return MarketData.get_top_tickers_by_volume(n)

        # 3. 스프레드 + 변동성 계산
        for c in candidates:
            ticker = c["ticker"]

            # 스프레드
            if self._ob_cache:
                c["spread_bps"] = self._ob_cache.get_spread_bps(ticker)
            else:
                c["spread_bps"] = 0.0  # 데이터 없으면 0 (스프레드 패널티 없음)

            # 일중 변동성 (high-low range / close)
            if c["close"] > 0 and c["high"] > 0:
                c["volatility"] = (c["high"] - c["low"]) / c["close"]
            else:
                c["volatility"] = 0.0

        # 3a. 스프레드 필터
        if self._ob_cache:
            candidates = [c for c in candidates if c["spread_bps"] <= self._max_spread_bps]

        if not candidates:
            logger.warning("[UniverseSelector] 스프레드 필터 후 종목 없음 → 거래대금만 사용")
            return MarketData.get_top_tickers_by_volume(n)

        # 4. 스코어 계산
        for c in candidates:
            log_value = math.log(max(c["value_24h"], 1))
            c["score"] = (
                _W_VALUE  * log_value
                + _W_VOL    * c["volatility"] * 100  # % 스케일로 변환
                + _W_SPREAD * c["spread_bps"]         # 음수 가중치이므로 높으면 감점
            )

        # 5. 정렬 + 상위 N개
        candidates.sort(key=lambda x: x["score"], reverse=True)
        result = [c["ticker"] for c in candidates[:n]]

        logger.info(
            f"[UniverseSelector] 종목 선정 완료 | "
            f"후보 {len(candidates)}개 → 상위 {n}개: {result[:5]}"
            + (f" … 외 {len(result) - 5}개" if len(result) > 5 else "")
        )

        # 디버그: 상위 5개 상세 점수
        for c in candidates[:5]:
            logger.debug(
                f"  {c['ticker']}: score={c['score']:.2f} "
                f"value={c['value_24h']/1e9:.1f}B "
                f"vol={c['volatility']*100:.2f}% "
                f"spread={c['spread_bps']:.1f}bps"
            )

        return result

    def compute_symbol_metrics(self, ticker: str) -> dict:
        """
        단일 종목의 SymbolMetrics 계산 (AutoTuner 입력용).

        반환 dict:
          atr, atr_pct, spread_bps, acc_trade_value_24h, last_close
        """
        result = {
            "ticker": ticker,
            "last_close": 0.0,
            "atr": 0.0,
            "atr_pct": 0.0,
            "spread_bps": 0.0,
            "acc_trade_value_24h": 0.0,
        }

        # ATR
        if self._md:
            try:
                atr = self._md.compute_atr(ticker, period=14, interval="minute60")
                df = self._md.get_ohlcv_intraday(ticker, "minute60", count=2)
                close = float(df["close"].iloc[-1]) if len(df) > 0 else 0.0
                result["atr"] = atr
                result["last_close"] = close
                result["atr_pct"] = atr / close if close > 0 else 0.0
            except DataFetchError:
                pass

        # 스프레드
        if self._ob_cache:
            result["spread_bps"] = self._ob_cache.get_spread_bps(ticker)

        return result

    @staticmethod
    def _fetch_ticker_data() -> list[dict]:
        """업비트 전체 KRW 마켓 시세 조회."""
        import pyupbit
        tickers = pyupbit.get_tickers(fiat="KRW")
        if not tickers:
            raise DataFetchError("KRW 종목 목록 조회 실패")

        url = "https://api.upbit.com/v1/ticker"
        all_data: list[dict] = []
        for i in range(0, len(tickers), 100):
            batch = tickers[i : i + 100]
            resp = requests.get(url, params={"markets": ",".join(batch)}, timeout=10)
            resp.raise_for_status()
            all_data.extend(resp.json())

        return all_data
