"""
동적 종목 선택 모듈 — DynamicTickerManager

업비트 24h 거래대금 기준 상위 N개 코인을 주기적으로 선택·갱신.
config.USE_DYNAMIC_TICKERS = True 시 Trader가 이 모듈을 사용.

사용법:
    mgr = DynamicTickerManager(n=10, blacklist=config.TICKER_BLACKLIST)
    tickers = mgr.get_current_tickers()           # 최초 로드 or 캐시 반환
    tickers, changed = mgr.refresh_if_needed()    # 만료 시 갱신
    mgr.n = 20                                    # 상위 N 동적 변경 (즉시 갱신 예약)
"""

import logging
import time

from data.market_data import MarketData

logger = logging.getLogger(__name__)


class DynamicTickerManager:
    """
    업비트 24h 거래대금 상위 N개 코인을 주기적으로 선택·갱신.

    Parameters
    ----------
    n : int
        상위 코인 수. 10 단위 권장 (10, 20, 30 … 100).
        10 단위가 아닌 경우 자동으로 내림 처리 (예: 15 → 10, 25 → 20).
    blacklist : list[str] | None
        추가 제외 종목 목록. config.TICKER_BLACKLIST 와 동기화 권장.
        MarketData.get_top_tickers_by_volume()의 기본 스테이블코인 블랙리스트도 적용됨.
    refresh_hours : float
        종목 목록 갱신 주기(시간). 기본 24시간.
    """

    # N 허용 범위 (Upbit KRW 마켓 종목 수 제한)
    _N_MIN: int = 10
    _N_MAX: int = 100
    _N_STEP: int = 10   # 10 단위 강제

    def __init__(
        self,
        n: int,
        blacklist: list[str] | None = None,
        refresh_hours: float = 24.0,
    ) -> None:
        self._n: int = self._clamp_n(n)
        self._blacklist: frozenset[str] = frozenset(blacklist or [])
        self._refresh_sec: float = refresh_hours * 3600.0
        self._tickers: list[str] = []
        self._last_refresh: float = 0.0

        logger.info(
            f"[DynamicTickerManager] 초기화 | "
            f"상위 {self._n}개 코인 | 갱신주기 {refresh_hours:.1f}h"
        )

    # ─── 외부 인터페이스 ────────────────────────────────────────────────────────

    def get_current_tickers(self) -> list[str]:
        """
        현재 종목 목록 반환.
        최초 호출 또는 캐시 만료 시 API로 자동 갱신.
        """
        if not self._tickers or self._is_expired():
            self._refresh()
        return list(self._tickers)

    def refresh_if_needed(
        self, custom_fetcher=None
    ) -> tuple[list[str], bool]:
        """
        갱신 주기가 만료됐으면 API로 갱신 후 (새 목록, True) 반환.
        만료 전이면 (현재 목록, False) 반환.

        Parameters
        ----------
        custom_fetcher : callable(n) → list[str], optional
            커스텀 종목 선정 함수 (예: UniverseSelector.select_top_n).
            None이면 기본 거래대금 순 정렬 사용.

        Returns
        -------
        tickers : list[str]
            현재 활성 종목 목록
        changed : bool
            갱신 여부 (True이면 종목 변경 발생 → WebSocket 재시작 필요)
        """
        if not self._tickers or self._is_expired():
            old_set = set(self._tickers)
            self._refresh(custom_fetcher=custom_fetcher)
            new_set = set(self._tickers)
            changed = new_set != old_set
            if changed:
                added   = new_set - old_set
                removed = old_set - new_set
                if added or removed:
                    logger.info(
                        f"[DynamicTickerManager] 종목 변경 감지 | "
                        f"추가={sorted(added)} | 제거={sorted(removed)}"
                    )
            return list(self._tickers), changed
        return list(self._tickers), False

    def force_refresh(self) -> list[str]:
        """강제 갱신 (갱신 주기 무시). 테스트·디버그용."""
        self._last_refresh = 0.0
        self._refresh()
        return list(self._tickers)

    # ─── 프로퍼티 ──────────────────────────────────────────────────────────────

    @property
    def n(self) -> int:
        """현재 설정된 상위 N 값"""
        return self._n

    @n.setter
    def n(self, value: int) -> None:
        """
        상위 N 값 동적 변경.
        10 단위로 자동 조정, 범위 초과 시 클램프.
        변경되면 다음 get_current_tickers() 호출 시 즉시 갱신.
        """
        clamped = self._clamp_n(value)
        if clamped != self._n:
            logger.info(
                f"[DynamicTickerManager] N 변경: {self._n} → {clamped} "
                f"(다음 루프에서 갱신 예약)"
            )
            self._n = clamped
            self._last_refresh = 0.0    # 즉시 갱신 예약

    @property
    def last_refresh_time(self) -> float:
        """마지막 갱신 Unix timestamp"""
        return self._last_refresh

    @property
    def next_refresh_in_sec(self) -> float:
        """다음 갱신까지 남은 시간(초). 0 이하이면 이미 만료."""
        remaining = self._refresh_sec - (time.time() - self._last_refresh)
        return max(0.0, remaining)

    # ─── 내부 ──────────────────────────────────────────────────────────────────

    def _is_expired(self) -> bool:
        return (time.time() - self._last_refresh) >= self._refresh_sec

    def _refresh(self, custom_fetcher=None) -> None:
        """
        API로 상위 N개 코인 조회 후 블랙리스트 제거.
        최초 로드 실패 시 RuntimeError 발생 (Trader 초기화 실패로 이어짐).
        이후 갱신 실패 시 이전 목록 유지 + 경고 로그.

        Parameters
        ----------
        custom_fetcher : callable(n) → list[str], optional
            커스텀 종목 선정 함수. None이면 기본 거래대금 사용.
        """
        try:
            if custom_fetcher:
                tickers = custom_fetcher(self._n)
            else:
                # MarketData.get_top_tickers_by_volume: 스테이블코인 기본 블랙리스트 이미 적용
                tickers = MarketData.get_top_tickers_by_volume(self._n)

            # config.TICKER_BLACKLIST 추가 필터 (혹시 누락된 종목 제거)
            tickers = [t for t in tickers if t not in self._blacklist]

            # 블랙리스트 제거 후 N개로 재제한
            self._tickers = tickers[: self._n]
            self._last_refresh = time.time()

            preview = self._tickers[:5]
            extra   = len(self._tickers) - len(preview)
            logger.info(
                f"[DynamicTickerManager] 종목 갱신 완료 | "
                f"상위 {self._n}개: {preview}"
                + (f" … 외 {extra}개" if extra > 0 else "")
            )

        except Exception as e:
            logger.error(f"[DynamicTickerManager] 종목 갱신 실패: {e}")
            if not self._tickers:
                raise RuntimeError(
                    f"동적 종목 최초 로드 실패. "
                    f"USE_DYNAMIC_TICKERS=False로 변경하거나 API 연결을 확인하세요. "
                    f"원인: {e}"
                ) from e
            # 갱신 실패 → 이전 목록 유지 (다음 갱신 주기까지 재시도 방지)
            self._last_refresh = time.time()
            logger.warning(
                f"[DynamicTickerManager] 이전 목록 유지 ({len(self._tickers)}개): {self._tickers[:5]}"
            )

    @classmethod
    def _clamp_n(cls, n: int) -> int:
        """n을 10 단위 + [N_MIN, N_MAX] 범위로 클램프."""
        stepped = (n // cls._N_STEP) * cls._N_STEP
        return max(cls._N_MIN, min(cls._N_MAX, stepped if stepped > 0 else cls._N_MIN))
