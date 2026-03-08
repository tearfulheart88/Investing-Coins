"""
세션 로그 저장/로드 유틸리티
=============================
거래 정지(실거래·가상거래) 시 각 전략(시나리오)의 거래 내역을
  logs/analysis/{scenario_id}_{real|paper}_{YYYY-MM-DD}_{HH-MM}.json
형식으로 저장한다.

Gemini 분석 요청 시 해당 폴더에서 가장 최근 로그를 불러와 분석에 활용.

저장 형식:
{
    "scenario_id": "vb_noise_filter",
    "saved_at":    "2026-03-08T09:00:00+09:00",
    "is_paper":    false,
    "summary":     { ... },   # account.get_summary() 또는 _build_obs_summary() 반환값
    "trades": [
        {
            "action":      "SELL",
            "ticker":      "KRW-BTC",
            "price":       95000000.0,
            "volume":      0.001,
            "timestamp":   "2026-03-08T09:00:00+09:00",
            "scenario_id": "vb_noise_filter",
            "reason":      "SCHEDULED_09H",
            "pnl_krw":     1200.0,
            "pnl_pct":     0.012        # ← ratio (0.012 = 1.2%, gemini_analyzer 규격)
        },
        ...
    ]
}

주의: pnl_pct는 항상 ratio(0.012 = 1.2%) 형식으로 저장.
      PaperTrade.pnl_pct (%) → 저장 시 /100 변환 후 저장.
      TradeRecord.pnl_pct (ratio) → 그대로 저장.
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

# 분석 로그 저장 폴더
ANALYSIS_DIR: str = os.path.join(config.LOGS_DIR, "analysis")


# ─── 저장 ────────────────────────────────────────────────────────────────────

def save_session_log(
    scenario_id: str,
    trades: list[dict],        # pnl_pct는 ratio (0.012 = 1.2%) 형식
    summary: dict,
    is_paper: bool = False,
) -> str | None:
    """
    세션 종료 시 분석용 로그를 ANALYSIS_DIR에 저장.

    Parameters
    ----------
    scenario_id : str
        전략 시나리오 ID (예: 'vb_noise_filter')
    trades : list[dict]
        거래 내역. pnl_pct는 반드시 ratio (0.012 = 1.2%).
    summary : dict
        세션 요약 (total_pnl, win_rate 등)
    is_paper : bool
        True = 가상거래, False = 실제거래

    Returns
    -------
    str | None
        저장된 파일 경로. 실패 시 None.
    """
    os.makedirs(ANALYSIS_DIR, exist_ok=True)
    now  = datetime.now(KST)
    mode = "paper" if is_paper else "real"
    filename = f"{scenario_id}_{mode}_{now.strftime('%Y-%m-%d_%H-%M')}.json"
    filepath = os.path.join(ANALYSIS_DIR, filename)

    data = {
        "scenario_id": scenario_id,
        "saved_at":    now.isoformat(),
        "is_paper":    is_paper,
        "summary":     summary,
        "trades":      trades,
    }

    try:
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2, default=str)
        logger.info(f"[SessionLog] 분석 로그 저장: {filename} ({len(trades)}건)")
        return filepath
    except Exception as e:
        logger.warning(f"[SessionLog] 분석 로그 저장 실패: {e}")
        return None


# ─── 로드 ────────────────────────────────────────────────────────────────────

def load_latest_session_log(
    scenario_id: str,
    max_trades: int = 50,
) -> list[dict]:
    """
    분석 폴더에서 해당 시나리오의 가장 최근 세션 로그를 로드.
    pnl_pct는 ratio (0.012 = 1.2%) 형식으로 반환 (gemini_analyzer 규격).

    Parameters
    ----------
    scenario_id : str
    max_trades  : int  최대 반환 건수

    Returns
    -------
    list[dict]  trades 목록. 데이터 없으면 빈 리스트.
    """
    if not os.path.exists(ANALYSIS_DIR):
        logger.debug(f"[SessionLog] 분석 폴더 없음: {ANALYSIS_DIR}")
        return []

    # scenario_id로 시작하는 .json 파일 목록
    prefix = f"{scenario_id}_"
    try:
        files = [
            f for f in os.listdir(ANALYSIS_DIR)
            if f.startswith(prefix) and f.endswith(".json")
        ]
    except OSError as e:
        logger.warning(f"[SessionLog] 분석 폴더 읽기 실패: {e}")
        return []

    if not files:
        logger.debug(f"[SessionLog] {scenario_id} 분석 로그 없음")
        return []

    # 파일명 형식: {scenario_id}_{mode}_{YYYY-MM-DD}_{HH-MM}.json
    # 역순 정렬(최신순)은 날짜가 파일명에 포함되어 있어 lexicographic 정렬로 정확
    files.sort(reverse=True)
    latest = os.path.join(ANALYSIS_DIR, files[0])

    try:
        with open(latest, "r", encoding="utf-8") as f:
            data = json.load(f)
        trades = data.get("trades", [])
        logger.info(
            f"[SessionLog] 분석 로그 로드: {files[0]} | {len(trades)}건"
        )
        return trades[-max_trades:]
    except Exception as e:
        logger.warning(f"[SessionLog] 분석 로그 로드 실패 ({files[0]}): {e}")
        return []


def list_session_logs(scenario_id: str | None = None) -> list[dict]:
    """
    분석 폴더의 세션 로그 목록 반환 (UI 표시용).

    Returns
    -------
    list[dict]  [{"filename": str, "scenario_id": str, "mode": str, "saved_at": str}, ...]
                최신순 정렬.
    """
    if not os.path.exists(ANALYSIS_DIR):
        return []

    result: list[dict] = []
    try:
        for fname in os.listdir(ANALYSIS_DIR):
            if not fname.endswith(".json"):
                continue
            if scenario_id and not fname.startswith(f"{scenario_id}_"):
                continue
            fpath = os.path.join(ANALYSIS_DIR, fname)
            try:
                with open(fpath, "r", encoding="utf-8") as f:
                    meta = json.load(f)
                result.append({
                    "filename":    fname,
                    "scenario_id": meta.get("scenario_id", ""),
                    "is_paper":    meta.get("is_paper", False),
                    "saved_at":    meta.get("saved_at", ""),
                    "trade_count": len(meta.get("trades", [])),
                })
            except Exception:
                continue
    except OSError:
        pass

    result.sort(key=lambda x: x["saved_at"], reverse=True)
    return result


# ─── PaperTrade → dict 변환 헬퍼 ────────────────────────────────────────────

def paper_trade_to_dict(trade) -> dict:
    """
    PaperTrade 객체 → Gemini 분석 규격 dict 변환.
    pnl_pct: PaperTrade는 % 단위(1.2) → ratio 단위(0.012)로 변환.
    """
    ts = trade.timestamp
    return {
        "action":      trade.action,
        "ticker":      trade.ticker,
        "price":       trade.price,
        "volume":      trade.volume,
        "timestamp":   ts.isoformat() if hasattr(ts, "isoformat") else str(ts),
        "scenario_id": trade.scenario_id,
        "reason":      trade.reason,
        "pnl_krw":     trade.pnl,
        "pnl_pct":     trade.pnl_pct / 100.0,   # % → ratio
    }
