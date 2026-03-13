from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from zoneinfo import ZoneInfo

from filelock import FileLock

import config

logger = logging.getLogger(__name__)
KST = ZoneInfo("Asia/Seoul")


def _mode_dir(is_paper: bool) -> str:
    return config.SIGNAL_TRACE_PAPER_DIR if is_paper else config.SIGNAL_TRACE_REAL_DIR


def append_signal_trace(trace: dict, *, is_paper: bool) -> str | None:
    """
    신호 판단 trace를 JSONL append-only 형태로 저장한다.

    실거래/가상거래를 분리하고, 날짜별 파일로 쌓아 비교/롤백 추적을 쉽게 만든다.
    """
    target_dir = _mode_dir(is_paper)
    os.makedirs(target_dir, exist_ok=True)

    now = datetime.now(KST)
    filename = f"signal_trace_{now.strftime('%Y-%m-%d')}.jsonl"
    filepath = os.path.join(target_dir, filename)
    lock = FileLock(filepath + ".lock")

    payload = {
        "saved_at": now.isoformat(),
        "mode": "paper" if is_paper else "real",
        **trace,
    }

    try:
        with lock:
            with open(filepath, "a", encoding="utf-8") as f:
                f.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")
        return filepath
    except Exception as exc:
        logger.warning(f"[SignalTrace] 저장 실패: {exc}")
        return None
