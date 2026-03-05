"""
거래 세션 시간 관리.
설정한 시간이 지나면 on_expire 콜백 호출.
"""
from __future__ import annotations
import logging
import threading
from datetime import datetime, timedelta
from typing import Callable, Optional

logger = logging.getLogger(__name__)

# 선택 가능 시간 옵션 (초)
DURATION_OPTIONS: dict[str, Optional[int]] = {
    "30분":   30 * 60,
    "1시간":  1 * 3600,
    "2시간":  2 * 3600,
    "6시간":  6 * 3600,
    "12시간": 12 * 3600,
    "24시간": 24 * 3600,
    "3일":    3 * 86400,
    "7일":    7 * 86400,
    "무제한": None,
}


class SessionTimer:

    def __init__(
        self,
        duration_sec: Optional[int],
        on_expire: Callable[[], None],
    ) -> None:
        self._duration   = duration_sec
        self._on_expire  = on_expire
        self._start_time: Optional[datetime] = None
        self._timer: Optional[threading.Timer]  = None

    def start(self) -> None:
        self._start_time = datetime.now()
        if self._duration:
            self._timer = threading.Timer(float(self._duration), self._expire)
            self._timer.daemon = True
            self._timer.start()
            end_time = self._start_time + timedelta(seconds=self._duration)
            logger.info(f"세션 타이머 시작 | 종료 예정: {end_time.strftime('%Y-%m-%d %H:%M:%S')}")
        else:
            logger.info("세션 타이머: 무제한")

    def stop(self) -> None:
        if self._timer:
            self._timer.cancel()
            self._timer = None

    def remaining_seconds(self) -> Optional[float]:
        if self._duration is None or self._start_time is None:
            return None
        elapsed = (datetime.now() - self._start_time).total_seconds()
        return max(0.0, self._duration - elapsed)

    def remaining_str(self) -> str:
        rem = self.remaining_seconds()
        if rem is None:
            return "무제한"
        h = int(rem // 3600)
        m = int((rem % 3600) // 60)
        s = int(rem % 60)
        return f"{h:02d}:{m:02d}:{s:02d}"

    def elapsed_str(self) -> str:
        if self._start_time is None:
            return "00:00:00"
        elapsed = (datetime.now() - self._start_time).total_seconds()
        h = int(elapsed // 3600)
        m = int((elapsed % 3600) // 60)
        s = int(elapsed % 60)
        return f"{h:02d}:{m:02d}:{s:02d}"

    def _expire(self) -> None:
        logger.info(f"세션 시간 만료 | 경과: {self.elapsed_str()}")
        self._on_expire()
