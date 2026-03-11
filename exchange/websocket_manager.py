import queue
import time
import threading
import logging
import pyupbit
from logging_.log_context import clear_log_mode, set_log_mode

logger = logging.getLogger(__name__)


class PriceCache:
    """
    Thread-safe 실시간 가격 캐시.
    WebSocket 스레드가 write, 메인 루프가 read.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._prices: dict[str, float] = {}
        self._timestamps: dict[str, float] = {}

    def update(self, ticker: str, price: float) -> None:
        with self._lock:
            self._prices[ticker] = price
            self._timestamps[ticker] = time.time()

    def get(self, ticker: str) -> float | None:
        with self._lock:
            return self._prices.get(ticker)

    def is_stale(self, ticker: str, max_age_sec: float = 10.0) -> bool:
        """마지막 업데이트 후 max_age_sec 초 초과 시 True (REST 폴백 필요)"""
        with self._lock:
            ts = self._timestamps.get(ticker, 0.0)
            return (time.time() - ts) > max_age_sec

    def all_prices(self) -> dict[str, float]:
        with self._lock:
            return dict(self._prices)

    @property
    def connected_tickers_count(self) -> int:
        """최근 가격 업데이트가 있는 종목 수"""
        with self._lock:
            cutoff = time.time() - 30.0
            return sum(1 for ts in self._timestamps.values() if ts > cutoff)


class WebSocketManager:
    """
    업비트 WebSocket 실시간 가격 피드.
    - daemon 스레드에서 실행 (main 종료 시 자동 종료)
    - 연결 끊김 시 지수 백오프 자동 재연결 (최대 60초)
    - .get() 타임아웃으로 종료 시그널 즉시 반응
    - 연결 상태 추적
    """

    _MAX_BACKOFF_SEC = 60.0
    _BASE_BACKOFF_SEC = 1.0
    _WS_GET_TIMEOUT_SEC = 2.0

    def __init__(
        self,
        tickers: list[str],
        cache: PriceCache,
        log_mode: str = "system",
    ) -> None:
        self._tickers = tickers
        self._cache = cache
        self._log_mode = log_mode
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._ws = None
        self._connected = False
        self._msg_count = 0

    @property
    def is_connected(self) -> bool:
        return self._connected

    @property
    def message_count(self) -> int:
        return self._msg_count

    def start(self) -> None:
        """백그라운드 daemon 스레드 시작"""
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run_loop,
            name=f"WebSocketPriceFeed-{self._log_mode}",
            daemon=True,
        )
        self._thread.start()
        logger.info(f"WebSocket 피드 시작 | 구독 종목: {self._tickers}")

    def stop(self) -> None:
        """스레드 종료 요청"""
        self._stop_event.set()
        self._connected = False
        if self._ws is not None:
            try:
                self._ws.terminate()
            except Exception:
                pass
        if self._thread is not None:
            self._thread.join(timeout=5.0)
        logger.info("WebSocket 피드 종료")

    def update_tickers(self, tickers: list[str]) -> bool:
        """
        구독 종목 목록 갱신.
        실행 중이면 같은 캐시를 유지한 채 재시작한다.
        """
        new_tickers = list(dict.fromkeys(tickers))
        if set(new_tickers) == set(self._tickers):
            return False

        was_running = self._thread is not None and self._thread.is_alive()
        if was_running:
            self.stop()

        self._tickers = new_tickers
        if was_running:
            self.start()

        logger.info(f"WebSocket 구독 종목 갱신: {self._tickers[:5]}")
        return True

    def _run_loop(self) -> None:
        set_log_mode(self._log_mode)
        backoff = self._BASE_BACKOFF_SEC
        fail_count = 0

        while not self._stop_event.is_set():
            try:
                logger.info("WebSocket 연결 시도...")
                self._ws = pyupbit.WebSocketManager("ticker", self._tickers)
                self._connected = True
                backoff = self._BASE_BACKOFF_SEC
                fail_count = 0
                logger.info("WebSocket 연결 성공")

                while not self._stop_event.is_set():
                    try:
                        # pyupbit.WebSocketManager.get()은 timeout 미지원 → 인자 없이 호출
                        msg = self._ws.get()
                    except Exception as e:
                        raise  # 외부 except로 전파 → backoff 적용

                    if msg is None:
                        continue

                    # pyupbit이 연결 끊김 시 문자열로 알림
                    if isinstance(msg, str) and "ConnectionClosed" in msg:
                        raise ConnectionError(f"WebSocket 서버 연결 끊김: {msg}")

                    ticker, price = self._parse_message(msg)
                    if ticker and price:
                        self._cache.update(ticker, price)
                        self._msg_count += 1

            except Exception as e:
                self._connected = False
                if self._stop_event.is_set():
                    break  # 종료 중이면 로그 없이 종료

                fail_count += 1
                if fail_count > 5:
                    logger.error(f"WebSocket 연속 실패 {fail_count}회, REST 폴백 유지: {e}")
                else:
                    logger.warning(f"WebSocket 끊김, {backoff:.0f}초 후 재연결: {e}")

                self._stop_event.wait(timeout=backoff)
                backoff = min(backoff * 2, self._MAX_BACKOFF_SEC)

            finally:
                self._connected = False
                if self._ws is not None:
                    try:
                        self._ws.terminate()
                    except Exception:
                        pass
                    self._ws = None
        clear_log_mode()

    def _parse_message(self, msg: dict) -> tuple[str | None, float | None]:
        """업비트 WebSocket ticker 메시지에서 (ticker, price) 추출"""
        try:
            ticker = msg.get("code") or msg.get("market")
            price = msg.get("trade_price") or msg.get("tradePrice")
            if ticker and price:
                return str(ticker), float(price)
        except (KeyError, TypeError, ValueError):
            pass
        return None, None
