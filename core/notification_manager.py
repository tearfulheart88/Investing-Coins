"""
주기적 거래 요약 알림.
- Windows 토스트 알림 (plyer)
- Obsidian 요약 파일 동시 생성
"""
from __future__ import annotations
import logging
import threading
from datetime import datetime
from typing import Callable

logger = logging.getLogger(__name__)


class NotificationManager:

    def __init__(
        self,
        interval_hours: float = 3.0,
        obsidian_logger=None,
    ) -> None:
        self._interval_sec    = interval_hours * 3600
        self._obsidian        = obsidian_logger
        self._stop_event      = threading.Event()
        self._thread: threading.Thread | None = None
        self._summary_fn: Callable[[], list[dict]] | None = None

    def set_summary_provider(self, fn: Callable[[], list[dict]]) -> None:
        """호출 시 list[dict] (account summaries) 를 반환하는 콜백 등록."""
        self._summary_fn = fn

    def start(self) -> None:
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="NotificationManager"
        )
        self._thread.start()
        logger.info(f"알림 매니저 시작 | 주기: {self._interval_sec / 3600:.1f}시간")

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=3.0)

    def send_now(self) -> None:
        """수동 즉시 요약 전송."""
        threading.Thread(target=self._send_summary, daemon=True).start()

    # ─── 내부 ────────────────────────────────────────────────────────────────

    def _run(self) -> None:
        while not self._stop_event.wait(timeout=self._interval_sec):
            self._send_summary()

    def _send_summary(self) -> None:
        if not self._summary_fn:
            return
        try:
            summaries = self._summary_fn()
            if not summaries:
                return

            # Obsidian 요약
            if self._obsidian:
                self._obsidian.write_summary(summaries)

            # Windows 알림
            msg = self._build_message(summaries)
            self._toast(msg)
            logger.info(f"요약 알림 전송 | {len(summaries)}개 계좌")
        except Exception as e:
            logger.warning(f"요약 알림 실패: {e}")

    def _build_message(self, summaries: list[dict]) -> str:
        now   = datetime.now().strftime("%H:%M")
        lines = [f"[{now}] 거래 요약"]
        for s in summaries:
            mode  = "가상" if s.get("is_paper") else "실제"
            sign  = "+" if s["total_pnl"] >= 0 else ""
            lines.append(
                f"[{mode}] {s['scenario_id']}: "
                f"{sign}{s['total_pnl']:,.0f}원 ({s['total_pnl_pct']:+.2f}%) "
                f"| {s['total_trades']}건"
            )
        return "\n".join(lines)

    def _toast(self, message: str) -> None:
        try:
            from plyer import notification
            notification.notify(
                title="Upbit 자동매매 요약",
                message=message,
                app_name="Upbit AutoTrader",
                timeout=15,
            )
        except Exception:
            logger.debug("토스트 알림 미지원 (plyer 미설치 또는 환경 미지원)")
