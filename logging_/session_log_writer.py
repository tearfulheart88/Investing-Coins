"""
세션 분석 로그 저장/로드 유틸리티.

거래 정지 시 각 시나리오의 최근 거래 내역을 JSON으로 저장한다.
실거래와 가상거래는 각각 아래 폴더로 분리된다.

- logs/analysis/real
- logs/analysis/paper

기존 루트 폴더(logs/analysis)에 남아 있는 레거시 파일도 로더에서 함께 읽는다.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from zoneinfo import ZoneInfo

import config

logger = logging.getLogger(__name__)
KST = ZoneInfo("Asia/Seoul")

ANALYSIS_DIR: str = config.ANALYSIS_DIR
ANALYSIS_REAL_DIR: str = config.ANALYSIS_REAL_DIR
ANALYSIS_PAPER_DIR: str = config.ANALYSIS_PAPER_DIR


def _mode_dir(is_paper: bool) -> str:
    return ANALYSIS_PAPER_DIR if is_paper else ANALYSIS_REAL_DIR


def _candidate_dirs(is_paper: bool | None = None) -> list[str]:
    dirs: list[str] = []
    if is_paper is None:
        dirs.extend([ANALYSIS_REAL_DIR, ANALYSIS_PAPER_DIR])
    else:
        dirs.append(_mode_dir(is_paper))

    legacy_root = os.path.normpath(ANALYSIS_DIR)
    if all(os.path.normpath(path) != legacy_root for path in dirs):
        dirs.append(ANALYSIS_DIR)
    return dirs


def _load_payload(path: str) -> dict | None:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.warning(f"[SessionLog] 분석 로그 로드 실패 ({os.path.basename(path)}): {e}")
        return None


def _collect_logs(
    scenario_id: str | None = None,
    is_paper: bool | None = None,
) -> list[tuple[str, dict]]:
    matches: list[tuple[str, dict]] = []
    seen: set[str] = set()
    prefix = f"{scenario_id}_" if scenario_id else None

    for directory in _candidate_dirs(is_paper):
        if not os.path.exists(directory):
            continue

        try:
            names = [name for name in os.listdir(directory) if name.endswith(".json")]
        except OSError as e:
            logger.warning(f"[SessionLog] 분석 폴더 읽기 실패: {directory} | {e}")
            continue

        for name in names:
            if prefix and not name.startswith(prefix):
                continue

            path = os.path.join(directory, name)
            norm = os.path.normpath(path)
            if norm in seen:
                continue
            seen.add(norm)

            payload = _load_payload(path)
            if not payload:
                continue
            if scenario_id and payload.get("scenario_id") != scenario_id:
                continue

            matches.append((path, payload))

    matches.sort(
        key=lambda item: (
            item[1].get("saved_at", ""),
            os.path.basename(item[0]),
        ),
        reverse=True,
    )
    return matches


def save_session_log(
    scenario_id: str,
    trades: list[dict],
    summary: dict,
    is_paper: bool = False,
    diagnostics: dict | None = None,
) -> str | None:
    """
    세션 종료 시 분석용 로그를 저장한다.

    Parameters
    ----------
    scenario_id : str
        전략 시나리오 ID
    trades : list[dict]
        거래 내역. pnl_pct는 ratio(0.012 = 1.2%) 형식.
    summary : dict
        세션 요약
    is_paper : bool
        True = 가상거래, False = 실제거래
    diagnostics : dict | None
        추적용 부가 메타데이터
    """
    mode = "paper" if is_paper else "real"
    target_dir = _mode_dir(is_paper)
    os.makedirs(target_dir, exist_ok=True)

    now = datetime.now(KST)
    filename = f"{scenario_id}_{mode}_{now.strftime('%Y-%m-%d_%H-%M')}.json"
    filepath = os.path.join(target_dir, filename)

    data = {
        "scenario_id": scenario_id,
        "saved_at": now.isoformat(),
        "is_paper": is_paper,
        "mode": mode,
        "summary": summary,
        "trades": trades,
        "diagnostics": diagnostics or {},
    }

    try:
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2, default=str)
        logger.info(f"[SessionLog] 분석 로그 저장: {filename} ({len(trades)}건)")
        return filepath
    except Exception as e:
        logger.warning(f"[SessionLog] 분석 로그 저장 실패: {e}")
        return None


def load_latest_session_log(
    scenario_id: str,
    max_trades: int = 50,
    is_paper: bool | None = None,
) -> list[dict] | None:
    """
    해당 시나리오의 가장 최근 세션 로그를 로드한다.

    is_paper를 지정하면 해당 모드 폴더만 조회하고,
    지정하지 않으면 real/paper/legacy를 모두 조회해 가장 최근 파일을 고른다.
    """
    matches = _collect_logs(scenario_id=scenario_id, is_paper=is_paper)
    if not matches:
        logger.debug(f"[SessionLog] {scenario_id} 분석 로그 없음")
        return None

    latest_path, payload = matches[0]
    trades = payload.get("trades", [])
    logger.info(
        f"[SessionLog] 분석 로그 로드: {os.path.basename(latest_path)} | {len(trades)}건"
    )
    return trades[-max_trades:]


def list_session_logs(
    scenario_id: str | None = None,
    is_paper: bool | None = None,
) -> list[dict]:
    """분석 로그 목록을 최신순으로 반환한다."""
    result: list[dict] = []
    for path, payload in _collect_logs(scenario_id=scenario_id, is_paper=is_paper):
        result.append({
            "filename": os.path.basename(path),
            "filepath": path,
            "scenario_id": payload.get("scenario_id", ""),
            "mode": payload.get("mode", "paper" if payload.get("is_paper") else "real"),
            "is_paper": payload.get("is_paper", False),
            "saved_at": payload.get("saved_at", ""),
            "trade_count": len(payload.get("trades", [])),
        })
    return result


def paper_trade_to_dict(trade) -> dict:
    """
    PaperTrade 유사 객체를 Gemini 분석 규격 dict로 변환한다.

    pnl_pct: PaperTrade는 % 단위(1.2), 저장 포맷은 ratio 단위(0.012).
    """
    ts = trade.timestamp
    return {
        "action": trade.action,
        "ticker": trade.ticker,
        "price": trade.price,
        "volume": trade.volume,
        "timestamp": ts.isoformat() if hasattr(ts, "isoformat") else str(ts),
        "scenario_id": trade.scenario_id,
        "reason": trade.reason,
        "pnl_krw": trade.pnl,
        "pnl_pct": trade.pnl_pct / 100.0,
    }
