"""
세션 기반 로깅 매니저 — SessionManager

거래 세션의 로그 생명주기를 관리.
세션 시작 시 전용 로그 디렉터리 + 파일 핸들러를 생성하고,
종료 시 모든 관련 로그를 결과 폴더에 수집.

■ 세션 ID 형식:
  {scenario_id}_{YYYY-MM-DD}_{HH-MM-SS}
  예: vb_noise_filter_2026-03-05_14-30-00

■ 세션 결과 폴더 구조:
  logs/sessions/{session_id}/
      system.log               # 세션 전용 시스템 로그 (실시간 기록)
      trades_session.jsonl     # 세션 거래만 추출 (종료 시)
      positions_snapshot.json  # 종료 시점 포지션 (종료 시)
      summary.json             # 세션 요약 — 전략, 시간, 수익률 (종료 시)

■ 사용법:
  sm = SessionManager(scenario_id="vb_noise_filter")
  sm.start()                    # 세션 시작
  # ... 거래 진행 ...
  sm.finalize(summary, positions)  # 세션 종료 + 결과 수집
"""

import os
import json
import logging
import threading
from datetime import datetime
from zoneinfo import ZoneInfo

import config

logger = logging.getLogger(__name__)

KST = ZoneInfo("Asia/Seoul")


class SessionManager:
    """
    거래 세션의 로그 생명주기 관리.

    - start(): 세션 ID 생성, 디렉터리 생성, root logger에 세션 파일 핸들러 추가
    - finalize(): 핸들러 제거, 세션 거래 추출, 스냅샷/요약 저장
    """

    def __init__(self, scenario_id: str) -> None:
        self._scenario_id = scenario_id
        self._session_id: str | None = None
        self._session_dir: str | None = None
        self._file_handler: logging.FileHandler | None = None
        self._start_time: datetime | None = None
        self._lock = threading.Lock()

    # ─── 프로퍼티 ──────────────────────────────────────────────────────────────

    @property
    def session_id(self) -> str | None:
        """현재 세션 ID. 미시작이면 None."""
        return self._session_id

    @property
    def session_dir(self) -> str | None:
        """현재 세션 결과 디렉터리 경로. 미시작이면 None."""
        return self._session_dir

    # ─── 세션 시작 ─────────────────────────────────────────────────────────────

    def start(self) -> str:
        """
        세션 시작.

        1. 세션 ID 생성: {scenario_id}_{YYYY-MM-DD}_{HH-MM-SS}
        2. 세션 디렉터리 생성: logs/sessions/{session_id}/
        3. root logger에 세션 전용 FileHandler 추가
           → 기존 전역 로그 + 세션 로그 동시 기록

        Returns
        -------
        session_id : str
        """
        self._start_time = datetime.now(KST)
        self._session_id = (
            f"{self._scenario_id}_"
            f"{self._start_time.strftime('%Y-%m-%d_%H-%M-%S')}"
        )
        self._session_dir = os.path.join(config.SESSIONS_DIR, self._session_id)
        os.makedirs(self._session_dir, exist_ok=True)

        # 세션 전용 파일 핸들러 (root logger에 추가)
        log_path = os.path.join(self._session_dir, "system.log")
        self._file_handler = logging.FileHandler(log_path, encoding="utf-8")
        self._file_handler.setLevel(logging.DEBUG)
        fmt = logging.Formatter(
            fmt="%(asctime)s [%(levelname)-8s] %(name)s | %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
        self._file_handler.setFormatter(fmt)
        logging.getLogger().addHandler(self._file_handler)

        logger.info(
            f"[SessionManager] 세션 시작 | "
            f"session_id={self._session_id} | "
            f"dir={self._session_dir}"
        )
        return self._session_id

    # ─── 세션 종료 + 결과 수집 ─────────────────────────────────────────────────

    def finalize(
        self,
        summary: dict,
        positions_snapshot: dict | list | None = None,
    ) -> str | None:
        """
        세션 종료: 결과 수집.

        1. root logger에서 세션 핸들러 제거
        2. trades.jsonl에서 이 세션의 거래만 추출 → trades_session.jsonl
        3. positions 스냅샷 저장 → positions_snapshot.json
        4. summary.json 저장 (전략, 시간, 수익률, 거래 통계)

        Parameters
        ----------
        summary : dict
            세션 요약 데이터 (Trader._build_obs_summary 결과).
        positions_snapshot : dict | list | None
            종료 시점 포지션 정보.

        Returns
        -------
        session_dir : str | None
            세션 결과 폴더 경로. 미시작 상태이면 None.
        """
        if not self._session_id or not self._session_dir:
            logger.warning("[SessionManager] finalize 호출 — 세션 미시작 상태")
            return None

        end_time = datetime.now(KST)

        logger.info(
            f"[SessionManager] 세션 종료 | "
            f"session_id={self._session_id} | "
            f"duration={(end_time - self._start_time).total_seconds():.0f}s"
        )

        # 1) 세션 핸들러 제거
        self._remove_file_handler()

        # 2) 세션 거래 추출
        self._extract_session_trades()

        # 3) positions 스냅샷 저장
        if positions_snapshot is not None:
            snap_path = os.path.join(self._session_dir, "positions_snapshot.json")
            try:
                with open(snap_path, "w", encoding="utf-8") as f:
                    json.dump(positions_snapshot, f, ensure_ascii=False, indent=2)
            except Exception as e:
                logger.warning(f"[SessionManager] 포지션 스냅샷 저장 실패: {e}")

        # 4) summary.json 저장
        summary_data = {
            "session_id": self._session_id,
            "scenario_id": self._scenario_id,
            "start_time": self._start_time.isoformat() if self._start_time else None,
            "end_time": end_time.isoformat(),
            "duration_sec": (end_time - self._start_time).total_seconds() if self._start_time else 0,
            **summary,
        }
        summary_path = os.path.join(self._session_dir, "summary.json")
        try:
            with open(summary_path, "w", encoding="utf-8") as f:
                json.dump(summary_data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.warning(f"[SessionManager] summary 저장 실패: {e}")

        logger.info(
            f"[SessionManager] 결과 수집 완료 | "
            f"dir={self._session_dir}"
        )
        return self._session_dir

    # ─── 내부 헬퍼 ─────────────────────────────────────────────────────────────

    def _remove_file_handler(self) -> None:
        """root logger에서 세션 파일 핸들러를 안전하게 제거."""
        with self._lock:
            if self._file_handler:
                try:
                    self._file_handler.flush()
                    self._file_handler.close()
                except Exception:
                    pass
                logging.getLogger().removeHandler(self._file_handler)
                self._file_handler = None

    def _extract_session_trades(self) -> None:
        """
        전역 trades.jsonl에서 이 세션의 거래만 추출.
        session_id 필드 매칭으로 필터링 → trades_session.jsonl로 저장.
        """
        jsonl_path = config.TRADES_JSON_PATH.replace(".json", ".jsonl")
        if not os.path.exists(jsonl_path):
            logger.debug("[SessionManager] trades.jsonl 없음 — 거래 추출 스킵")
            return

        output_path = os.path.join(self._session_dir, "trades_session.jsonl")
        count = 0
        try:
            with open(jsonl_path, "r", encoding="utf-8") as src, \
                 open(output_path, "w", encoding="utf-8") as dst:
                for line in src:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        record = json.loads(line)
                        if record.get("session_id") == self._session_id:
                            dst.write(line + "\n")
                            count += 1
                    except json.JSONDecodeError:
                        continue

            if count > 0:
                logger.info(f"[SessionManager] 세션 거래 추출: {count}건 → {output_path}")
            else:
                logger.debug("[SessionManager] 이 세션의 거래 기록 없음")

        except Exception as e:
            logger.warning(f"[SessionManager] 거래 추출 실패: {e}")
