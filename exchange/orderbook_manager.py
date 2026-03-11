"""
호가창(Orderbook) WebSocket 매니저 — OrderbookManager

업비트 WebSocket으로 실시간 호가 데이터를 수신하여:
  - 스프레드(bps) 계산
  - 최우선 매수/매도 호가 제공
  - 호가 깊이(depth) 요약

기존 WebSocketManager(체결가 피드)와 독립적으로 운영.
스프레드 데이터는 AutoTuner, UniverseSelector가 참조.
"""

import time
import threading
import logging
import json
from dataclasses import dataclass
from logging_.log_context import clear_log_mode, set_log_mode

logger = logging.getLogger(__name__)


@dataclass
class OrderbookSnapshot:
    """단일 종목의 호가 스냅샷."""
    ticker: str
    best_ask: float = 0.0      # 최우선 매도가 (1호가)
    best_bid: float = 0.0      # 최우선 매수가 (1호가)
    ask_size: float = 0.0      # 매도 1호가 수량
    bid_size: float = 0.0      # 매수 1호가 수량
    mid_price: float = 0.0     # (ask + bid) / 2
    spread_bps: float = 0.0    # 10000 × (ask - bid) / mid
    total_ask_size: float = 0.0   # 매도 총 잔량
    total_bid_size: float = 0.0   # 매수 총 잔량
    timestamp: float = 0.0     # 수신 시각 (unix ts)


class OrderbookCache:
    """
    Thread-safe 호가 캐시.
    OrderbookManager가 write, 메인루프/AutoTuner가 read.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._data: dict[str, OrderbookSnapshot] = {}

    def update(self, snapshot: OrderbookSnapshot) -> None:
        with self._lock:
            self._data[snapshot.ticker] = snapshot

    def get(self, ticker: str) -> OrderbookSnapshot | None:
        with self._lock:
            return self._data.get(ticker)

    def get_spread_bps(self, ticker: str) -> float:
        """스프레드(bps) 반환. 데이터 없으면 999.0 (진입 차단)."""
        with self._lock:
            snap = self._data.get(ticker)
            if snap is None or snap.mid_price <= 0:
                return 999.0
            # 30초 이상 오래된 데이터면 stale
            if time.time() - snap.timestamp > 30.0:
                return 999.0
            return snap.spread_bps

    def get_best_prices(self, ticker: str) -> tuple[float, float]:
        """(best_bid, best_ask) 반환. 없으면 (0, 0)."""
        with self._lock:
            snap = self._data.get(ticker)
            if snap is None:
                return 0.0, 0.0
            return snap.best_bid, snap.best_ask

    def all_snapshots(self) -> dict[str, OrderbookSnapshot]:
        with self._lock:
            return dict(self._data)


class OrderbookManager:
    """
    업비트 WebSocket 호가(orderbook) 피드.
    - daemon 스레드에서 실행
    - 지수 백오프 자동 재연결
    - 종목별 OrderbookSnapshot을 OrderbookCache에 저장
    """

    _MAX_BACKOFF_SEC = 60.0
    _BASE_BACKOFF_SEC = 2.0

    def __init__(
        self,
        tickers: list[str],
        cache: OrderbookCache,
        log_mode: str = "system",
    ) -> None:
        self._tickers = tickers
        self._cache = cache
        self._log_mode = log_mode
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._connected = False

    @property
    def is_connected(self) -> bool:
        return self._connected

    def start(self) -> None:
        """백그라운드 daemon 스레드 시작."""
        if not self._tickers:
            logger.info("[OrderbookManager] 구독 종목 없음 → 시작 안 함")
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run_loop,
            name=f"OrderbookFeed-{self._log_mode}",
            daemon=True,
        )
        self._thread.start()
        logger.info(f"[OrderbookManager] 시작 | 종목: {self._tickers[:5]}")

    def stop(self) -> None:
        self._stop_event.set()
        self._connected = False
        if self._thread is not None:
            self._thread.join(timeout=5.0)
        logger.info("[OrderbookManager] 종료")

    def _run_loop(self) -> None:
        import pyupbit

        set_log_mode(self._log_mode)
        backoff = self._BASE_BACKOFF_SEC
        ws = None

        while not self._stop_event.is_set():
            try:
                ws = pyupbit.WebSocketManager("orderbook", self._tickers)
                self._connected = True
                backoff = self._BASE_BACKOFF_SEC

                while not self._stop_event.is_set():
                    try:
                        msg = ws.get()
                    except Exception:
                        raise

                    if msg is None:
                        continue
                    if isinstance(msg, str) and "ConnectionClosed" in msg:
                        raise ConnectionError(f"Orderbook WS 끊김: {msg}")

                    self._process_message(msg)

            except Exception as e:
                self._connected = False
                if self._stop_event.is_set():
                    break
                logger.warning(f"[OrderbookManager] 연결 오류, {backoff:.0f}초 후 재연결: {e}")
                self._stop_event.wait(timeout=backoff)
                backoff = min(backoff * 2, self._MAX_BACKOFF_SEC)

            finally:
                self._connected = False
                if ws is not None:
                    try:
                        ws.terminate()
                    except Exception:
                        pass
                    ws = None
        clear_log_mode()

    def _process_message(self, msg: dict) -> None:
        """업비트 orderbook 메시지 파싱 → OrderbookSnapshot."""
        try:
            ticker = msg.get("code") or msg.get("market")
            if not ticker:
                return

            units = msg.get("orderbook_units") or msg.get("obu") or []
            if not units:
                return

            # 1호가 (best bid/ask)
            best = units[0]
            best_ask  = float(best.get("ask_price", 0) or best.get("ap", 0))
            best_bid  = float(best.get("bid_price", 0) or best.get("bp", 0))
            ask_size  = float(best.get("ask_size",  0) or best.get("as", 0))
            bid_size  = float(best.get("bid_size",  0) or best.get("bs", 0))

            if best_ask <= 0 or best_bid <= 0:
                return

            mid = (best_ask + best_bid) / 2.0
            spread_bps = 10000.0 * (best_ask - best_bid) / mid if mid > 0 else 999.0

            # 호가 총 잔량
            total_ask = float(msg.get("total_ask_size", 0) or msg.get("tas", 0))
            total_bid = float(msg.get("total_bid_size", 0) or msg.get("tbs", 0))

            snapshot = OrderbookSnapshot(
                ticker=str(ticker),
                best_ask=best_ask,
                best_bid=best_bid,
                ask_size=ask_size,
                bid_size=bid_size,
                mid_price=mid,
                spread_bps=spread_bps,
                total_ask_size=total_ask,
                total_bid_size=total_bid,
                timestamp=time.time(),
            )
            self._cache.update(snapshot)

        except (KeyError, TypeError, ValueError) as e:
            logger.debug(f"[OrderbookManager] 파싱 오류: {e}")
