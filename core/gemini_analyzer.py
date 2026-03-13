"""
Gemini API 전략 분석기 — GeminiStrategyAnalyzer

현재 활성 전략 소스코드 + 최근 거래 로그를 Gemini API에 전송하여
전략 개선점을 분석하고, Claude에게 전달할 프롬프트(JSON)를 생성합니다.

■ 사용법:
  analyzer = GeminiStrategyAnalyzer(api_key="AIza...")
  result   = analyzer.analyze(scenario_id="vb_noise_filter", max_trades=50)
  # result: dict (claude_prompt_json 필드 포함)
"""
from __future__ import annotations

import json
import os
import logging
from datetime import datetime
from zoneinfo import ZoneInfo
from typing import Optional

import config

logger = logging.getLogger(__name__)
KST = ZoneInfo("Asia/Seoul")

# ─── 전략 파일 매핑 ────────────────────────────────────────────────────────────
SCENARIO_TO_FILE: dict[str, str] = {
    "vb_noise_filter":        "strategies/vb_noise_filter.py",
    "vb_standard":            "strategies/vb_standard.py",
    "mr_rsi":                 "strategies/mr_rsi.py",
    "mr_bollinger":           "strategies/mr_bollinger.py",
    "scalping_triple_ema":    "strategies/scalping_triple_ema.py",
    "scalping_bb_rsi":        "strategies/scalping_bb_rsi.py",
    "scalping_5ema_reversal": "strategies/scalping_5ema_reversal.py",
    "macd_rsi_trend":         "strategies/macd_rsi_trend.py",
    "smrh_stop":              "strategies/smrh_stop.py",
}


class GeminiStrategyAnalyzer:
    """
    Gemini API를 사용해 거래 전략을 분석하고
    Claude용 개선 프롬프트(JSON)를 생성합니다.
    """

    def __init__(self, api_key: str) -> None:
        self._api_key = api_key
        self._model_name = "gemini-2.5-flash"  # 최신 안정 모델 (무료 티어 지원)
        self._base_dir   = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    # ─── 공개 API ─────────────────────────────────────────────────────────────

    def analyze(
        self,
        scenario_id: str,
        max_trades: int = 50,
        session_id: Optional[str] = None,
    ) -> dict:
        """
        전략 분석 실행.

        Parameters
        ----------
        scenario_id : str
            분석 대상 시나리오 ID (예: "vb_noise_filter")
        max_trades : int
            분석에 사용할 최근 거래 기록 수 (기본 50)
        session_id : str | None
            특정 세션 ID로 필터링. None이면 최근 max_trades건 사용.

        Returns
        -------
        dict
            {
              "analysis_timestamp": str,
              "scenario_id": str,
              "trade_count": int,
              "win_rate": float,
              "avg_pnl_pct": float,
              "gemini_analysis": str,           # Gemini 원문 분석
              "issues": list[str],              # 발견된 문제점
              "improvements": list[str],         # 개선 제안
              "claude_prompt_json": dict,        # Claude에 전달할 프롬프트
            }
        """
        try:
            import google.generativeai as genai  # type: ignore
        except ImportError:
            raise ImportError(
                "google-generativeai 패키지가 필요합니다.\n"
                "pip install google-generativeai"
            )

        genai.configure(api_key=self._api_key)
        model = genai.GenerativeModel(self._model_name)

        # 1) 전략 소스코드 로드
        strategy_code = self._load_strategy_code(scenario_id)

        # 2) 거래 기록 / 세션 문맥 로드
        session_payload = self._load_latest_session_payload(scenario_id) if not session_id else None
        trades = self._load_trades(scenario_id, max_trades, session_id)
        analysis_context = self._build_analysis_context(scenario_id, trades, session_payload)

        # 3) 거래 통계 계산
        stats = self._compute_stats(trades, analysis_context)

        # 4) Gemini 프롬프트 구성
        prompt = self._build_gemini_prompt(
            scenario_id,
            strategy_code,
            trades,
            stats,
            analysis_context,
        )

        # 5) Gemini API 호출
        logger.info(f"[GeminiAnalyzer] Gemini API 호출 | scenario={scenario_id} | trades={len(trades)}")
        try:
            response = model.generate_content(prompt)
            raw_text = response.text
        except Exception as e:
            raise RuntimeError(f"Gemini API 호출 실패: {e}") from e

        # 6) 응답 파싱
        parsed = self._parse_gemini_response(raw_text)

        # 7) Claude 프롬프트 JSON 생성
        claude_prompt = self._build_claude_prompt(
            scenario_id, strategy_code, trades, stats, parsed, analysis_context
        )

        result = {
            "analysis_timestamp": datetime.now(KST).isoformat(),
            "scenario_id":        scenario_id,
            "trade_count":        stats["total"],
            "win_rate":           stats["win_rate"],
            "avg_pnl_pct":        stats["avg_pnl_pct"],
            "best_pnl_pct":       stats["best_pnl_pct"],
            "worst_pnl_pct":      stats["worst_pnl_pct"],
            "gemini_analysis":    raw_text,
            "issues":             parsed.get("issues", []),
            "improvements":       parsed.get("improvements", []),
            "analysis_context":   analysis_context,
            "claude_prompt_json": claude_prompt,
        }

        logger.info(f"[GeminiAnalyzer] 분석 완료 | issues={len(result['issues'])}개")
        return result

    # ─── 데이터 로드 ──────────────────────────────────────────────────────────

    def _load_strategy_code(self, scenario_id: str) -> str:
        """전략 파이썬 소스코드 로드."""
        rel_path = SCENARIO_TO_FILE.get(scenario_id)
        if not rel_path:
            logger.warning(f"[GeminiAnalyzer] 전략 파일 매핑 없음: {scenario_id}")
            return f"# 전략 파일 없음: {scenario_id}"

        full_path = os.path.join(self._base_dir, rel_path)
        if not os.path.exists(full_path):
            return f"# 파일 없음: {rel_path}"

        try:
            with open(full_path, "r", encoding="utf-8") as f:
                return f.read()
        except Exception as e:
            return f"# 파일 읽기 실패: {e}"

    def _load_trades(
        self,
        scenario_id: str,
        max_trades: int,
        session_id: Optional[str],
    ) -> list[dict]:
        """
        거래 기록 로드 — 우선순위:
          1) logs/analysis/ 폴더 (가장 최근 세션 로그)  ← 정지 후 저장되는 파일
          2) trades.jsonl 폴백 (실거래 누적 JSONL)
        """
        # ── [1순위] 분석 로그 폴더에서 최신 세션 로드 ────────────────────────
        if not session_id:  # 세션 ID 지정 없을 때만 (지정 시 JSONL 사용)
            try:
                from logging_.session_log_writer import load_latest_session_log
                session_trades = load_latest_session_log(scenario_id, max_trades)
                if session_trades is not None:
                    logger.info(
                        f"[GeminiAnalyzer] 분석 로그 폴더에서 로드 | "
                        f"scenario={scenario_id} | {len(session_trades)}건"
                    )
                    return session_trades
            except Exception as e:
                logger.warning(f"[GeminiAnalyzer] 분석 로그 폴더 로드 실패: {e}")

        # ── [2순위] trades.jsonl 폴백 ─────────────────────────────────────────
        jsonl_path = config.TRADES_JSON_PATH.replace(".json", ".jsonl")
        if not os.path.exists(jsonl_path):
            logger.warning(
                "[GeminiAnalyzer] trades.jsonl 없음 — 거래 정지 후 다시 분석하세요."
            )
            return []

        try:
            records: list[dict] = []
            with open(jsonl_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue

                    # 시나리오 필터
                    if rec.get("scenario_id") != scenario_id:
                        continue
                    # 세션 필터 (지정 시)
                    if session_id and rec.get("session_id") != session_id:
                        continue
                    # 에러 거래 제외
                    if rec.get("error"):
                        continue
                    records.append(rec)

            logger.info(
                f"[GeminiAnalyzer] trades.jsonl에서 로드 | "
                f"scenario={scenario_id} | {len(records[-max_trades:])}건"
            )
            return records[-max_trades:]

        except Exception as e:
            logger.warning(f"[GeminiAnalyzer] 거래 기록 로드 실패: {e}")
            return []

    def _load_latest_session_payload(self, scenario_id: str) -> dict | None:
        """최신 세션 payload 전체를 불러온다."""
        try:
            from logging_.session_log_writer import load_latest_session_payload
            payload = load_latest_session_payload(scenario_id)
            if payload is not None:
                logger.info(f"[GeminiAnalyzer] 최신 세션 payload 로드 | scenario={scenario_id}")
            return payload
        except Exception as e:
            logger.warning(f"[GeminiAnalyzer] 최신 세션 payload 로드 실패: {e}")
            return None

    def _load_live_open_positions(self, scenario_id: str) -> list[dict]:
        """현재 positions.json 에서 시나리오별 오픈 포지션을 불러온다."""
        path = getattr(config, "POSITIONS_FILE", "")
        if not path or not os.path.exists(path):
            return []

        try:
            with open(path, "r", encoding="utf-8") as f:
                payload = json.load(f)
        except Exception as e:
            logger.warning(f"[GeminiAnalyzer] positions.json 로드 실패: {e}")
            return []

        positions = payload.get("positions", {}) or {}
        result: list[dict] = []
        for ticker, pos in positions.items():
            if pos.get("scenario_id") != scenario_id:
                continue
            normalized = {
                "ticker": ticker,
                "buy_price": pos.get("buy_price"),
                "stop_loss_price": pos.get("stop_loss_price"),
                "take_profit_price": pos.get("take_profit_price"),
                "locked_profit_price": pos.get("locked_profit_price"),
                "buy_time": pos.get("buy_time"),
                "order_uuid": pos.get("order_uuid"),
                "strategy_id": pos.get("strategy_id"),
                "is_external_sync": (
                    pos.get("order_uuid") == "EXCHANGE_SYNC"
                    or pos.get("strategy_id") == "exchange_sync"
                ),
            }
            normalized["age_hours"] = self._compute_age_hours(normalized.get("buy_time"))
            result.append(normalized)
        return result

    def _compute_age_hours(self, buy_time: str | None) -> float | None:
        if not buy_time:
            return None
        try:
            dt = datetime.fromisoformat(buy_time)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=KST)
            return round((datetime.now(KST) - dt.astimezone(KST)).total_seconds() / 3600, 3)
        except Exception:
            return None

    def _build_analysis_context(
        self,
        scenario_id: str,
        trades: list[dict],
        session_payload: dict | None,
    ) -> dict:
        summary = (session_payload or {}).get("summary", {}) or {}
        diagnostics = (session_payload or {}).get("diagnostics", {}) or {}

        open_positions = diagnostics.get("open_positions") or self._load_live_open_positions(scenario_id)
        normalized_open_positions: list[dict] = []
        for pos in open_positions:
            normalized = {
                "ticker": pos.get("ticker"),
                "buy_price": pos.get("buy_price"),
                "stop_loss_price": pos.get("stop_loss_price"),
                "take_profit_price": pos.get("take_profit_price"),
                "locked_profit_price": pos.get("locked_profit_price"),
                "buy_time": pos.get("buy_time"),
                "order_uuid": pos.get("order_uuid"),
                "strategy_id": pos.get("strategy_id"),
                "is_external_sync": bool(pos.get("is_external_sync", False)),
            }
            normalized["age_hours"] = self._compute_age_hours(normalized.get("buy_time"))
            normalized_open_positions.append(normalized)

        notes: list[str] = []
        if normalized_open_positions and not any(t.get("action") == "SELL" for t in trades):
            notes.append(
                "최근 세션에 완료 매도가 없더라도 오픈 포지션이 존재하면 매도 기능 미작동으로 단정하면 안 됩니다."
            )

        suspicious_buy_reasons = sorted({
            t.get("reason") for t in trades
            if t.get("action") == "BUY"
            and isinstance(t.get("reason"), str)
            and t.get("reason", "").startswith("SMRH_STOP(")
        })
        if suspicious_buy_reasons:
            notes.append(
                "최근 세션 로그에 다른 전략 계열 매수 사유가 섞여 보입니다. 멀티시나리오 세션 로그 오염 가능성을 먼저 점검해야 합니다."
            )

        recent_signal_traces = (diagnostics.get("recent_signal_traces") or [])[-10:]
        return {
            "session_saved_at": (session_payload or {}).get("saved_at"),
            "mode": (session_payload or {}).get("mode"),
            "session_summary": summary,
            "trade_action_counts": diagnostics.get("trade_action_counts", {}),
            "open_position_count": len(normalized_open_positions),
            "open_positions": normalized_open_positions,
            "recent_signal_traces": recent_signal_traces,
            "notes": notes,
        }

    # ─── 통계 ─────────────────────────────────────────────────────────────────

    def _compute_stats(self, trades: list[dict], analysis_context: dict | None = None) -> dict:
        """거래 통계 계산."""
        perf_trades = [
            t for t in trades
            if t.get("action") in {"BUY", "SELL"}
        ]
        strategy_trades = [
            t for t in perf_trades
            if t.get("strategy_id") not in {"exchange_sync", "unknown"}
        ]
        sells = [t for t in strategy_trades if t.get("action") == "SELL"]
        buys  = [t for t in strategy_trades if t.get("action") == "BUY"]

        pnl_list = [
            t["pnl_pct"] for t in sells
            if t.get("pnl_pct") is not None
        ]
        wins = [p for p in pnl_list if p > 0]

        # reason 분포
        reasons: dict[str, int] = {}
        for t in sells:
            r = t.get("reason", "UNKNOWN")
            reasons[r] = reasons.get(r, 0) + 1

        buy_reasons: dict[str, int] = {}
        for t in buys:
            r = t.get("reason", "UNKNOWN")
            buy_reasons[r] = buy_reasons.get(r, 0) + 1

        context = analysis_context or {}

        return {
            "total":       len(strategy_trades),
            "buy_count":   len(buys),
            "sell_count":  len(sells),
            "win_rate":    round(len(wins) / len(pnl_list) * 100, 1) if pnl_list else 0.0,
            "avg_pnl_pct": round(sum(pnl_list) / len(pnl_list) * 100, 3) if pnl_list else 0.0,
            "best_pnl_pct":  round(max(pnl_list) * 100, 3) if pnl_list else 0.0,
            "worst_pnl_pct": round(min(pnl_list) * 100, 3) if pnl_list else 0.0,
            "total_pnl_krw": round(sum(
                t.get("pnl_krw", 0) or 0 for t in sells
            ), 0),
            "exit_reasons": reasons,
            "buy_reasons": buy_reasons,
            "reentry_count": sum(1 for t in trades if t.get("action") == "REENTRY"),
            "excluded_external_count": len(perf_trades) - len(strategy_trades),
            "open_position_count": context.get("open_position_count", 0),
            "has_open_positions": bool(context.get("open_position_count", 0)),
            "analysis_notes": context.get("notes", []),
        }

    # ─── Gemini 프롬프트 ──────────────────────────────────────────────────────

    def _build_gemini_prompt(
        self,
        scenario_id: str,
        strategy_code: str,
        trades: list[dict],
        stats: dict,
        analysis_context: dict,
    ) -> str:
        """Gemini API에 전달할 분석 프롬프트 생성."""
        # 거래 요약 (토큰 절약을 위해 핵심 필드만)
        trade_summary = [
            {
                "ts":     t.get("timestamp", "")[:16],
                "action": t.get("action"),
                "ticker": t.get("ticker"),
                "price":  t.get("price"),
                "pnl_pct": round(t["pnl_pct"] * 100, 2) if t.get("pnl_pct") is not None else None,
                "reason": t.get("reason"),
            }
            for t in trades[-30:]   # 최대 30건
        ]

        config_params = config.STRATEGY_PARAMS.get(
            scenario_id.replace("vb_noise_filter", "vb").replace("vb_standard", "vb"),
            {}
        )

        prompt = f"""당신은 암호화폐 자동매매 전략 전문가입니다.
아래 Python 전략 코드와 최근 거래 데이터를 분석하여 개선점을 찾아주세요.

=== 전략 정보 ===
시나리오 ID: {scenario_id}
현재 설정 파라미터:
{json.dumps(config_params, ensure_ascii=False, indent=2)}

=== 거래 성과 통계 ===
{json.dumps(stats, ensure_ascii=False, indent=2)}

=== 최신 세션 문맥 ===
{json.dumps(analysis_context, ensure_ascii=False, indent=2)}

=== 최근 거래 내역 (최대 30건) ===
{json.dumps(trade_summary, ensure_ascii=False, indent=2)}

=== 전략 소스코드 ===
```python
{strategy_code[:6000]}
```

=== 분석 요청 ===
위 정보를 바탕으로 다음 형식의 JSON으로만 답변해주세요 (다른 텍스트 없이):

{{
  "summary": "전략 성과 1~2문장 요약",
  "issues": [
    "발견된 문제점 1",
    "발견된 문제점 2"
  ],
  "improvements": [
    "구체적 개선안 1 (파라미터 값 포함)",
    "구체적 개선안 2"
  ],
  "param_suggestions": {{
    "파라미터명": "제안값 + 이유"
  }},
  "risk_warnings": [
    "주의해야 할 리스크 사항"
  ]
}}

분석 기준:
- 승률, 평균 수익률, 매도 이유 분포를 핵심 지표로 사용
- 구체적인 파라미터 조정값 제안 (예: k_min=0.35→0.25)
- 한국어로 작성
- sell_count가 0이어도 open_position_count가 1 이상이면 "매도 기능 미작동"으로 단정하지 말 것
- requires_scheduled_sell=False 는 09:00 예약 청산 미사용 뜻이지, 루프 내 should_sell_on_signal 비활성화 의미가 아님
- 제공된 코드에 실제로 없는 내용만 "누락"으로 지적할 것
- 다른 전략 사유가 세션 로그에 섞여 보이면 로그 오염 가능성을 먼저 지적하고, 해당 사유를 이 전략 고유 로직으로 단정하지 말 것
- JSON 형식만 반환 (마크다운 코드블록 없이)"""

        return prompt

    # ─── 응답 파싱 ────────────────────────────────────────────────────────────

    def _parse_gemini_response(self, raw_text: str) -> dict:
        """Gemini 응답에서 JSON 추출."""
        text = raw_text.strip()

        # 마크다운 코드블록 제거
        if text.startswith("```"):
            lines = text.splitlines()
            text = "\n".join(
                l for l in lines
                if not l.strip().startswith("```")
            ).strip()

        try:
            return json.loads(text)
        except json.JSONDecodeError:
            # JSON 파싱 실패 시 원문을 summary로 저장
            return {
                "summary":          raw_text[:500],
                "issues":           ["Gemini 응답 파싱 실패 — 원문 참조"],
                "improvements":     [],
                "param_suggestions": {},
                "risk_warnings":    [],
            }

    # ─── Claude 프롬프트 생성 ─────────────────────────────────────────────────

    def _build_claude_prompt(
        self,
        scenario_id: str,
        strategy_code: str,
        trades: list[dict],
        stats: dict,
        parsed: dict,
        analysis_context: dict,
    ) -> dict:
        """
        Claude에게 전달할 프롬프트 JSON 생성.
        Claude Code / API 모두에서 바로 사용 가능한 구조.
        """
        system_msg = (
            "당신은 Python 암호화폐 자동매매 전략 코드 전문가입니다. "
            "제공된 거래 데이터와 전략 코드를 분석하고, "
            "구체적이고 실행 가능한 코드 수준의 개선안을 제시해 주세요."
        )

        user_content = f"""아래 Upbit 자동매매 전략의 개선 작업을 요청합니다.

## 전략 정보
- 시나리오: `{scenario_id}`
- 분석 시점: {datetime.now(KST).strftime('%Y-%m-%d %H:%M KST')}

## Gemini 사전 분석 결과
**요약**: {parsed.get('summary', '—')}

**발견된 문제점**:
{chr(10).join(f'- {i}' for i in parsed.get('issues', []))}

**개선 제안**:
{chr(10).join(f'- {i}' for i in parsed.get('improvements', []))}

**파라미터 조정 제안**:
{json.dumps(parsed.get('param_suggestions', {}), ensure_ascii=False, indent=2)}

## 거래 성과 데이터
```json
{json.dumps(stats, ensure_ascii=False, indent=2)}
```

## 최신 세션 문맥
```json
{json.dumps(analysis_context, ensure_ascii=False, indent=2)}
```

## 현재 전략 파라미터
```python
{json.dumps(config.STRATEGY_PARAMS, ensure_ascii=False, indent=2)}
```

## 전략 소스코드 (핵심 부분)
```python
{strategy_code[:4000]}
```

## 작업 요청
1. 위 Gemini 분석을 검토하고 누락된 개선점을 추가 발굴해주세요
2. 실제 파이썬 코드 수준에서 수정이 필요한 부분을 구체적으로 제시해주세요
3. 파라미터 최적화 제안 시 근거 데이터(승률, 평균수익)를 인용해주세요
4. 리스크 관리 측면에서 개선 가능한 부분을 알려주세요
5. 가능하다면 수정된 전략 코드 스니펫을 제공해주세요"""

        return {
            "schema_version": "1.0",
            "generated_at":   datetime.now(KST).isoformat(),
            "scenario_id":    scenario_id,
            "performance_snapshot": stats,
            "analysis_context": analysis_context,
            "gemini_pre_analysis":  parsed,
            "claude_request": {
                "system": system_msg,
                "messages": [
                    {
                        "role":    "user",
                        "content": user_content,
                    }
                ],
                "model":       "claude-opus-4-5",
                "max_tokens":  8192,
                "temperature": 0.3,
            },
        }
