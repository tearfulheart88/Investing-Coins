import time
import threading
import logging
import requests
from datetime import date, datetime
from zoneinfo import ZoneInfo
import numpy as np
import pandas as pd
import pyupbit

from exchange.upbit_client import DataFetchError

logger = logging.getLogger(__name__)

KST = ZoneInfo("Asia/Seoul")

# ─── 스테이블코인 블랙리스트 ──────────────────────────────────────────────────
_TICKER_BLACKLIST: frozenset[str] = frozenset({
    "KRW-USDT", "KRW-USDC", "KRW-DAI", "KRW-BUSD",
})


class MarketData:
    """
    OHLCV 조회 및 기술 지표 계산 모듈.
    - OHLCV 데이터는 당일 기준으로 캐시 (count 인식)
    - 09:01 KST에 스케줄러가 invalidate_cache() 호출
    - 각 전략이 필요한 지표를 여기서만 계산 (전략 파일 내 계산 로직 없음)
    """

    _INTRADAY_CACHE_SEC: float  = 30.0   # 분봉 캐시 유효 시간(초)
    _MAX_INTRADAY_COUNT: int   = 200    # Upbit API 최대 캔들 수
    _MIN_API_INTERVAL:   float = 0.12   # 연속 API 호출 최소 간격(초) — rate limit 방어

    def __init__(self) -> None:
        # 일봉 캐시: ticker → (df, cache_date, cached_count)
        self._cache: dict[str, tuple[pd.DataFrame, date, int]] = {}
        # 분봉 캐시: (ticker, interval) → (df, fetch_timestamp)
        self._intraday_cache: dict[tuple, tuple[pd.DataFrame, float]] = {}
        # API rate limit 제어
        self._api_lock = threading.Lock()
        self._last_api_call: float = 0.0

    # ─── 일봉 OHLCV ──────────────────────────────────────────────────────────

    def get_ohlcv(self, ticker: str, count: int = 30) -> pd.DataFrame:
        """
        일봉 OHLCV DataFrame 반환.
        캐시된 데이터가 있으면 같은 날짜 + 충분한 count인 경우 재사용.
        """
        today = datetime.now(KST).date()
        cached = self._cache.get(ticker)
        if cached:
            df, cache_date, cached_count = cached
            if cache_date == today and cached_count >= count:
                return df

        df = self._fetch_ohlcv(ticker, count)
        self._cache[ticker] = (df, today, count)
        return df

    def _fetch_ohlcv(self, ticker: str, count: int) -> pd.DataFrame:
        """OHLCV API 호출 (재시도 포함) — _api_lock + None 재시도 sleep 적용"""
        for attempt in range(3):
            try:
                # rate limit: 분봉과 동일한 직렬화 + 최소 간격 보장
                with self._api_lock:
                    elapsed = time.time() - self._last_api_call
                    if elapsed < self._MIN_API_INTERVAL:
                        time.sleep(self._MIN_API_INTERVAL - elapsed)
                    df = pyupbit.get_ohlcv(ticker, interval="day", count=count)
                    self._last_api_call = time.time()

                if df is not None and not df.empty:
                    return df

                # API가 None/empty 반환 → 잠시 대기 후 재시도
                if attempt < 2:
                    time.sleep(1.0 + attempt)

            except Exception as e:
                if attempt < 2:
                    time.sleep(1.5 ** attempt)
                    continue
                raise DataFetchError(f"OHLCV 조회 실패: {ticker} - {e}") from e

        raise DataFetchError(f"OHLCV 데이터 없음: {ticker}")

    def invalidate_cache(self, ticker: str | None = None) -> None:
        """캐시 수동 무효화. ticker=None 이면 전체 무효화 (09:01 스케줄용)"""
        if ticker:
            self._cache.pop(ticker, None)
        else:
            self._cache.clear()
        logger.info(f"OHLCV 캐시 무효화: {ticker or '전체'}")

    # ─── 변동성 돌파 (VB 계열) ─────────────────────────────────────────────────

    def _get_completed_candles(self, ticker: str, count: int) -> pd.DataFrame:
        """
        완성된 캔들만 반환 (오늘 미완성 캔들 제외).
        09:00 KST 이전이면 마지막 캔들이 아직 미완성이므로 제외.
        """
        # 여유 있게 count+3 요청
        df = self.get_ohlcv(ticker, count=count + 3)
        if len(df) < 2:
            raise DataFetchError(f"OHLCV 데이터 부족: {ticker}")

        # pyupbit 일봉: 마지막 행이 오늘(아직 진행 중) 또는 가장 최근 완성 캔들
        # 업비트 일봉은 09:00 KST 기준이므로, 마지막 행 = 오늘 09:00에 시작된 캔들
        # → 항상 마지막 행은 미완성으로 간주하고 제외
        completed = df.iloc[:-1]
        return completed

    def compute_noise_filter_k(self, ticker: str, days: int = 5) -> float:
        """
        노이즈 필터 k 계산.
        noise_i = abs(open_i - close_i) / (high_i - low_i)
        k = mean(1 - noise_i)  for 최근 days개 완성 캔들
        반환: float [0.1, 0.9] (클램프)
        """
        completed = self._get_completed_candles(ticker, days)
        recent = completed.iloc[-days:]

        if len(recent) < days:
            raise DataFetchError(f"노이즈 k 계산에 필요한 데이터 부족: {ticker} ({len(recent)}/{days})")

        ranges = recent["high"] - recent["low"]
        valid = ranges > 0
        if not valid.any():
            logger.warning(f"유효 캔들 없음 (범위=0), k=0.5 기본값 사용: {ticker}")
            return 0.5

        noise = (recent["open"] - recent["close"]).abs() / ranges
        noise = noise[valid]
        k = float((1 - noise).mean())
        return max(0.1, min(0.9, k))

    def compute_target_price(self, ticker: str, k: float) -> float:
        """
        변동성 돌파 목표가.
        target = today_open + yesterday_range * k
        yesterday: 마지막 완성 캔들, today: 현재 진행 중 캔들
        """
        df = self.get_ohlcv(ticker, count=5)
        if len(df) < 2:
            raise DataFetchError(f"목표가 계산 데이터 부족: {ticker}")

        today = df.iloc[-1]       # 현재 진행 중 캔들 (오늘 시가 포함)
        yesterday = df.iloc[-2]   # 마지막 완성 캔들

        yesterday_range = float(yesterday["high"]) - float(yesterday["low"])
        today_open = float(today["open"])
        return today_open + yesterday_range * k

    # ─── 이동평균선 ──────────────────────────────────────────────────────────

    def compute_ma(self, ticker: str, period: int = 15) -> float:
        """단순 이동평균 (종가 기준)"""
        df = self.get_ohlcv(ticker, count=period + 5)
        closes = df["close"].dropna()
        if len(closes) < period:
            raise DataFetchError(f"MA 계산 데이터 부족: {ticker} (필요={period}, 보유={len(closes)})")
        return float(closes.iloc[-period:].mean())

    # ─── RSI (mr_rsi 전략용) ──────────────────────────────────────────────────

    def compute_rsi(self, ticker: str, period: int = 14) -> float:
        """
        RSI(period) 계산.
        반환: float [0, 100]
        """
        df = self.get_ohlcv(ticker, count=period * 3)
        closes = df["close"].dropna()
        if len(closes) < period + 1:
            raise DataFetchError(f"RSI 계산 데이터 부족: {ticker}")

        delta = closes.diff().dropna()
        gain = delta.clip(lower=0)
        loss = (-delta).clip(lower=0)

        avg_gain = gain.ewm(com=period - 1, adjust=False).mean().iloc[-1]
        avg_loss = loss.ewm(com=period - 1, adjust=False).mean().iloc[-1]

        if avg_loss == 0:
            return 100.0
        rs = avg_gain / avg_loss
        return float(100 - (100 / (1 + rs)))

    # ─── 볼린저 밴드 (mr_bollinger 전략용) ───────────────────────────────────

    def compute_bollinger(
        self, ticker: str, period: int = 20, std_mult: float = 2.0
    ) -> tuple[float, float, float]:
        """
        볼린저 밴드 계산.
        반환: (upper, middle, lower)
        """
        df = self.get_ohlcv(ticker, count=period + 5)
        closes = df["close"].dropna()
        if len(closes) < period:
            raise DataFetchError(f"볼린저 밴드 계산 데이터 부족: {ticker}")

        recent = closes.iloc[-period:]
        middle = float(recent.mean())
        std = float(recent.std())
        upper = middle + std_mult * std
        lower = middle - std_mult * std
        return upper, middle, lower

    # ─── 거래량 상위 종목 ─────────────────────────────────────────────────────

    @staticmethod
    def get_top_tickers_by_volume(n: int = 100) -> list[str]:
        """
        업비트 KRW 마켓에서 24시간 거래대금 기준 상위 n개 종목 반환.
        """
        # 1. 전체 KRW 종목 목록
        tickers = pyupbit.get_tickers(fiat="KRW")
        if not tickers:
            raise DataFetchError("KRW 종목 목록 조회 실패")

        # 2. 배치(100개)로 24h 시세 조회
        url = "https://api.upbit.com/v1/ticker"
        all_data: list[dict] = []
        for i in range(0, len(tickers), 100):
            batch = tickers[i : i + 100]
            resp = requests.get(
                url,
                params={"markets": ",".join(batch)},
                timeout=10,
            )
            resp.raise_for_status()
            all_data.extend(resp.json())

        # 3. 24h 거래대금(acc_trade_price_24h) 기준 내림차순 정렬
        all_data.sort(
            key=lambda x: float(x.get("acc_trade_price_24h") or 0),
            reverse=True,
        )
        # 4. 스테이블코인 블랙리스트 제외
        return [
            d["market"] for d in all_data
            if d["market"] not in _TICKER_BLACKLIST
        ][:n]

    # ─── 분봉 OHLCV (스캘핑 전략용) ─────────────────────────────────────────

    def get_ohlcv_intraday(
        self, ticker: str, interval: str = "minute5", count: int = 100
    ) -> pd.DataFrame:
        """
        분봉 OHLCV DataFrame 반환.
        interval: "minute1" | "minute5" | "minute15" | "minute30" | "minute60" | "minute240"
        캐시 유효시간 30초 (실시간성 유지).

        캐시 정책:
          - 같은 (ticker, interval) 키 → 30초 내 요청이면 캐시 반환
          - 요청 count가 캐시된 df보다 크면 → 새로 fetch 후 캐시 덮어쓰기
          - Upbit API 최대 200봉 제한은 _fetch_ohlcv_intraday에서 자동 적용
        """
        cache_key = (ticker, interval)
        now_ts = time.time()
        cached = self._intraday_cache.get(cache_key)
        if cached:
            df, ts = cached
            # 유효 시간 내 + 충분한 봉 수 → 캐시 반환
            if now_ts - ts < self._INTRADAY_CACHE_SEC and len(df) >= count:
                return df

        df = self._fetch_ohlcv_intraday(ticker, interval, count)
        self._intraday_cache[cache_key] = (df, now_ts)
        return df

    def _fetch_ohlcv_intraday(
        self, ticker: str, interval: str, count: int
    ) -> pd.DataFrame:
        # Upbit API 최대 200캔들 제한 준수
        capped_count = min(count, self._MAX_INTRADAY_COUNT)

        for attempt in range(3):
            try:
                # rate limit: 동시 호출 직렬화 + 최소 간격 보장
                with self._api_lock:
                    elapsed = time.time() - self._last_api_call
                    if elapsed < self._MIN_API_INTERVAL:
                        time.sleep(self._MIN_API_INTERVAL - elapsed)
                    df = pyupbit.get_ohlcv(ticker, interval=interval, count=capped_count)
                    self._last_api_call = time.time()

                if df is not None and not df.empty:
                    return df

                # API가 None/empty 반환 → 잠시 대기 후 재시도
                if attempt < 2:
                    time.sleep(1.0 + attempt)

            except Exception as e:
                if attempt < 2:
                    time.sleep(1.5 ** attempt)
                    continue
                raise DataFetchError(
                    f"분봉 OHLCV 조회 실패: {ticker}/{interval} - {e}"
                ) from e

        raise DataFetchError(f"분봉 OHLCV 데이터 없음: {ticker}/{interval}")

    # ─── EMA (스캘핑 전략용) ─────────────────────────────────────────────────

    def compute_ema_df(
        self, ticker: str, periods: list[int], interval: str = "minute5"
    ) -> pd.DataFrame:
        """
        여러 EMA를 한 번에 계산.
        반환: DataFrame (columns: open, high, low, close, volume, ema{p1}, ema{p2}, ...)

        interval 기본값 "minute5" (1분봉 노이즈 방지 — scalping 전략 기준).
        volume 컬럼 보존: 전략에서 거래량 급증 판단에 직접 활용 가능.
        """
        max_p = max(periods)
        # API 제한(200) 준수: max_p*2 or 200 중 작은 값 (EMA 워밍업 최소 확보)
        fetch_count = min(max(max_p * 2, 100), self._MAX_INTRADAY_COUNT)
        df = self.get_ohlcv_intraday(ticker, interval, count=fetch_count)
        closes = df["close"].dropna().reset_index(drop=True)

        # open/high/low/close/volume 포함 (volume은 거래량 급증 판단에 사용)
        keep_cols = [c for c in ["open", "high", "low", "close", "volume"] if c in df.columns]
        out_df = df[keep_cols].copy().reset_index(drop=True)

        if len(closes) < max_p:
            raise DataFetchError(
                f"EMA 계산 데이터 부족: {ticker} interval={interval} "
                f"(필요={max_p}, 보유={len(closes)})"
            )
        for p in periods:
            out_df[f"ema{p}"] = closes.ewm(span=p, adjust=False).mean()
        return out_df

    # ─── 분봉 RSI ─────────────────────────────────────────────────────────────

    def compute_rsi_intraday(
        self, ticker: str, period: int = 14, interval: str = "minute60"
    ) -> float:
        """분봉 기반 RSI 계산."""
        df = self.get_ohlcv_intraday(ticker, interval, count=period * 4)
        closes = df["close"].dropna()
        if len(closes) < period + 1:
            raise DataFetchError(f"분봉 RSI 데이터 부족: {ticker}/{interval}")

        delta = closes.diff().dropna()
        gain = delta.clip(lower=0)
        loss = (-delta).clip(lower=0)
        avg_gain = gain.ewm(com=period - 1, adjust=False).mean().iloc[-1]
        avg_loss = loss.ewm(com=period - 1, adjust=False).mean().iloc[-1]

        if avg_loss == 0:
            return 100.0
        return float(100 - (100 / (1 + avg_gain / avg_loss)))

    # ─── 분봉 볼린저 밴드 ────────────────────────────────────────────────────

    def compute_bollinger_intraday(
        self,
        ticker: str,
        period: int = 20,
        std_mult: float = 2.0,
        interval: str = "minute60",
    ) -> tuple[float, float, float]:
        """분봉 기반 볼린저 밴드 (upper, middle, lower)."""
        df = self.get_ohlcv_intraday(ticker, interval, count=period + 20)
        closes = df["close"].dropna()
        if len(closes) < period:
            raise DataFetchError(f"분봉 볼린저 데이터 부족: {ticker}/{interval}")

        recent = closes.iloc[-period:]
        middle = float(recent.mean())
        std = float(recent.std(ddof=1))
        return middle + std_mult * std, middle, middle - std_mult * std

    # ─── ADX ─────────────────────────────────────────────────────────────────

    def compute_adx(
        self, ticker: str, period: int = 14, interval: str = "minute15"
    ) -> float:
        """
        ADX(period) 계산.
        반환: float [0, 100]. 데이터 부족 시 25.0 반환 (중립 값).
        """
        count = max(period * 4, 60)
        df = self.get_ohlcv_intraday(ticker, interval, count=count)
        if len(df) < period + 2:
            return 25.0

        high  = df["high"].reset_index(drop=True)
        low   = df["low"].reset_index(drop=True)
        close = df["close"].reset_index(drop=True)

        prev_close = close.shift(1)
        tr = pd.concat([
            high - low,
            (high - prev_close).abs(),
            (low  - prev_close).abs(),
        ], axis=1).max(axis=1)

        up   = high.diff()
        down = -(low.diff())
        plus_dm  = up.where((up > down)   & (up   > 0), 0.0)
        minus_dm = down.where((down > up) & (down > 0), 0.0)

        atr       = tr.ewm(span=period, adjust=False).mean()
        plus_di   = 100 * (plus_dm.ewm(span=period, adjust=False).mean()  / atr.replace(0, float("nan")))
        minus_di  = 100 * (minus_dm.ewm(span=period, adjust=False).mean() / atr.replace(0, float("nan")))
        sum_di    = (plus_di + minus_di).replace(0, float("nan"))
        dx        = 100 * (plus_di - minus_di).abs() / sum_di
        adx       = dx.ewm(span=period, adjust=False).mean()

        val = adx.iloc[-1]
        return float(val) if pd.notna(val) else 25.0

    # ─── MACD ────────────────────────────────────────────────────────────────

    def compute_macd(
        self,
        ticker: str,
        fast: int = 12,
        slow: int = 26,
        signal: int = 9,
        interval: str = "minute60",
    ) -> dict:
        """
        MACD 계산.
        반환 dict:
          "macd":       MACD 라인 현재값
          "signal_val": 시그널 라인 현재값
          "hist":       히스토그램 현재값  (macd - signal)
          "hist_prev":  히스토그램 1봉 전
          "hist_prev2": 히스토그램 2봉 전
        """
        # EMA 수렴을 위해 slow * 3 캔들 필요, API 200 캡 준수
        count = min(slow * 3 + signal * 2, self._MAX_INTRADAY_COUNT)
        df = self.get_ohlcv_intraday(ticker, interval, count=count)
        closes = df["close"].dropna().reset_index(drop=True)

        if len(closes) < slow + signal + 3:
            raise DataFetchError(
                f"MACD 데이터 부족: {ticker}/{interval} "
                f"(필요≥{slow + signal + 3}, 보유={len(closes)})"
            )

        ema_f   = closes.ewm(span=fast,   adjust=False).mean()
        ema_s   = closes.ewm(span=slow,   adjust=False).mean()
        macd_l  = ema_f - ema_s
        sig_l   = macd_l.ewm(span=signal, adjust=False).mean()
        hist    = macd_l - sig_l

        return {
            "macd":        float(macd_l.iloc[-1]),
            "macd_prev":   float(macd_l.iloc[-2]),   # MACD 라인 1봉 전 (골든/데드크로스 판정용)
            "signal_val":  float(sig_l.iloc[-1]),
            "signal_prev": float(sig_l.iloc[-2]),    # 시그널 라인 1봉 전
            "hist":        float(hist.iloc[-1]),
            "hist_prev":   float(hist.iloc[-2]),
            "hist_prev2":  float(hist.iloc[-3]),
            "hist_prev3":  float(hist.iloc[-4]) if len(hist) >= 4 else float(hist.iloc[-3]),
        }

    # ─── 단일 EMA 스칼라 (HTF 추세 필터용) ──────────────────────────────────

    def compute_ema_intraday(
        self, ticker: str, period: int = 200, interval: str = "minute240"
    ) -> float:
        """
        단일 EMA 스칼라 반환 (주로 EMA(200, 4h) 장기 추세 필터에 사용).
        반환: float — 현재 EMA 값
        데이터 부족 시 DataFetchError 발생.
        """
        # Upbit API 최대 200봉 제한 준수
        fetch_count = min(max(period + 30, 100), self._MAX_INTRADAY_COUNT)
        df = self.get_ohlcv_intraday(ticker, interval, count=fetch_count)
        closes = df["close"].dropna().reset_index(drop=True)

        if len(closes) < min(period, fetch_count - 5):
            raise DataFetchError(
                f"EMA({period}) 데이터 부족: {ticker}/{interval} "
                f"(필요≈{period}, 보유={len(closes)})"
            )

        ema = closes.ewm(span=period, adjust=False).mean()
        return float(ema.iloc[-1])

    # ─── RSI 시리즈 (최근 N개) ──────────────────────────────────────────────

    def compute_rsi_series_intraday(
        self,
        ticker: str,
        period: int = 14,
        interval: str = "minute60",
        n: int = 3,
    ) -> list[float]:
        """
        최근 n개의 RSI 값을 리스트로 반환.
        rsi_series[-1] = 현재, rsi_series[-2] = 1봉 전, ...
        """
        # period * 4 + n 만큼 확보
        count = min(period * 4 + n, self._MAX_INTRADAY_COUNT)
        df = self.get_ohlcv_intraday(ticker, interval, count=count)
        closes = df["close"].dropna().reset_index(drop=True)

        if len(closes) < period + n:
            raise DataFetchError(
                f"RSI 시리즈 데이터 부족: {ticker}/{interval} "
                f"(필요≥{period + n}, 보유={len(closes)})"
            )

        delta    = closes.diff()
        gain     = delta.clip(lower=0)
        loss     = (-delta).clip(lower=0)
        avg_gain = gain.ewm(com=period - 1, adjust=False).mean()
        avg_loss = loss.ewm(com=period - 1, adjust=False).mean()

        # 0으로 나누기 방지
        rs  = avg_gain / avg_loss.replace(0, float("nan"))
        rsi = (100 - (100 / (1 + rs))).fillna(100.0)

        # 최근 n개 반환
        return [float(rsi.iloc[-(n - i)]) for i in range(n - 1, -1, -1)]

    # ─── Heikin-Ashi ─────────────────────────────────────────────────────────

    @staticmethod
    def _heikin_ashi(df: pd.DataFrame) -> pd.DataFrame:
        """
        일반 OHLCV → Heikin-Ashi 변환.
        HA_close = (O + H + L + C) / 4
        HA_open  = (prev_HA_open + prev_HA_close) / 2  (첫 봉 = (O0+C0)/2)
        HA_high  = max(H, HA_open, HA_close)
        HA_low   = min(L, HA_open, HA_close)
        """
        o = df["open"].to_numpy(dtype=float)
        h = df["high"].to_numpy(dtype=float)
        l = df["low"].to_numpy(dtype=float)
        c = df["close"].to_numpy(dtype=float)
        n = len(o)

        ha_c = (o + h + l + c) / 4.0
        ha_o = np.empty(n)
        ha_o[0] = (o[0] + c[0]) / 2.0
        for i in range(1, n):
            ha_o[i] = (ha_o[i - 1] + ha_c[i - 1]) / 2.0
        ha_h = np.maximum(h, np.maximum(ha_o, ha_c))
        ha_l = np.minimum(l, np.minimum(ha_o, ha_c))

        result = df.copy().reset_index(drop=True)
        result["open"]  = ha_o
        result["high"]  = ha_h
        result["low"]   = ha_l
        result["close"] = ha_c
        return result

    def compute_ha_intraday(
        self, ticker: str, interval: str = "minute30", count: int = 50
    ) -> pd.DataFrame:
        """
        분봉 Heikin-Ashi DataFrame 반환.
        columns: open, high, low, close, is_bullish, turned_bullish
          - is_bullish:     HA_close > HA_open
          - turned_bullish: 이전 봉 음봉 → 현재 봉 양봉 (양봉전환)
        """
        # HA 계산에는 최소 2봉 필요 (이전 봉 참조)
        df = self.get_ohlcv_intraday(ticker, interval, count=count + 2)
        if len(df) < 3:
            raise DataFetchError(f"HA 계산 데이터 부족: {ticker}/{interval}")

        ha = self._heikin_ashi(df)
        is_bull = ha["close"] > ha["open"]
        ha["is_bullish"]    = is_bull
        ha["turned_bullish"] = (~is_bull.shift(1).fillna(True).infer_objects(copy=False).astype(bool)) & is_bull
        return ha

    # ─── Stochastic Oscillator ────────────────────────────────────────────────

    def compute_stochastic(
        self,
        ticker: str,
        k_period: int  = 12,
        d_period: int  = 3,
        smooth_k: int  = 3,
        interval: str  = "minute30",
    ) -> dict:
        """
        Stochastic Oscillator 계산.
        %K_raw = 100 × (close - lowest_low(k_period)) / (highest_high(k_period) - lowest_low(k_period))
        %K     = SMA(%K_raw, smooth_k)
        %D     = SMA(%K, d_period)

        반환 dict:
          "k":      현재 %K
          "d":      현재 %D
          "k_prev": 1봉 전 %K
          "d_prev": 1봉 전 %D
        돌파 판정 (호출부): k_prev <= d_prev AND k > d
        """
        need = k_period + d_period + smooth_k + 5
        count = min(need, self._MAX_INTRADAY_COUNT)
        df = self.get_ohlcv_intraday(ticker, interval, count=count)

        if len(df) < k_period + d_period + 2:
            raise DataFetchError(
                f"Stochastic 데이터 부족: {ticker}/{interval} "
                f"(필요≥{k_period + d_period + 2}, 보유={len(df)})"
            )

        high  = df["high"].reset_index(drop=True)
        low   = df["low"].reset_index(drop=True)
        close = df["close"].reset_index(drop=True)

        lowest_low   = low.rolling(k_period).min()
        highest_high = high.rolling(k_period).max()
        hl_range     = (highest_high - lowest_low).replace(0.0, float("nan"))
        k_raw        = 100.0 * (close - lowest_low) / hl_range

        k = k_raw.rolling(smooth_k).mean() if smooth_k > 1 else k_raw
        d = k.rolling(d_period).mean()

        return {
            "k":      float(k.iloc[-1]),
            "d":      float(d.iloc[-1]),
            "k_prev": float(k.iloc[-2]),
            "d_prev": float(d.iloc[-2]),
        }

    # ─── ATR ─────────────────────────────────────────────────────────────────

    def compute_atr(
        self, ticker: str, period: int = 14, interval: str = "minute15"
    ) -> float:
        """ATR(period) 계산."""
        count = max(period * 3, 50)
        df = self.get_ohlcv_intraday(ticker, interval, count=count)
        if len(df) < period + 1:
            raise DataFetchError(f"ATR 데이터 부족: {ticker}/{interval}")

        high  = df["high"].reset_index(drop=True)
        low   = df["low"].reset_index(drop=True)
        close = df["close"].reset_index(drop=True)
        prev_close = close.shift(1)

        tr = pd.concat([
            high - low,
            (high - prev_close).abs(),
            (low  - prev_close).abs(),
        ], axis=1).max(axis=1)

        atr = tr.ewm(span=period, adjust=False).mean()
        return float(atr.iloc[-1])

    # ─── ATR% (AutoTuner 입력용) ────────────────────────────────────────────

    def compute_atr_pct(
        self, ticker: str, period: int = 14, interval: str = "minute60"
    ) -> tuple[float, float, float]:
        """
        ATR%  = ATR / last_close.
        AutoTuner에서 변동성 분류(low/medium/high)에 사용.

        반환: (atr, atr_pct, last_close)
        """
        atr = self.compute_atr(ticker, period=period, interval=interval)
        df = self.get_ohlcv_intraday(ticker, interval, count=2)
        close = float(df["close"].iloc[-1]) if len(df) > 0 else 0.0
        atr_pct = atr / close if close > 0 else 0.0
        return atr, atr_pct, close

    # ─── 거래량 SMA ───────────────────────────────────────────────────────────

    def compute_volume_sma_intraday(
        self, ticker: str, period: int = 20, interval: str = "minute60"
    ) -> tuple[float, float]:
        """
        현재 봉 거래량과 최근 period봉 평균 거래량 반환.
        반환: (current_volume, volume_sma)
        거래량 급증 판단: current_volume >= volume_sma * mult
        """
        df = self.get_ohlcv_intraday(ticker, interval, count=period + 5)
        vol = df["volume"].reset_index(drop=True)
        if len(vol) < period + 1:
            raise DataFetchError(
                f"거래량 SMA 데이터 부족: {ticker}/{interval} "
                f"(필요≥{period + 1}, 보유={len(vol)})"
            )
        vol_sma = float(vol.rolling(period).mean().iloc[-1])
        vol_cur = float(vol.iloc[-1])
        return vol_cur, vol_sma
