"""
옵시디언 볼트에 거래 기록을 마크다운으로 저장.

기본 경로: {project_root}/logs/obsidian/   ← vault_path 미지정 시 자동 사용
별도 볼트 경로를 지정하면 해당 경로에 저장.

폴더 구조:
  {vault}/{folder}/{mode}_{scenario_id}/{YYYY-MM-DD}.md   ← 거래 건별
  {vault}/{folder}/요약/{YYYY-MM-DD_HH-MM}.md              ← 3시간 요약
  {vault}/{folder}/세션/세션_{timestamp}_시작.md            ← 세션 시작
  {vault}/{folder}/세션/세션_{timestamp}_종료.md            ← 세션 종료
  {vault}/{folder}/진단/{scenario_id}_진단.md              ← 신호 차단 진단
"""
from __future__ import annotations
import os
import threading
import logging
from datetime import date, datetime

logger = logging.getLogger(__name__)

# 기본 볼트: 이 파일 기준 두 단계 위 → logs/obsidian/
_DEFAULT_VAULT = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "logs", "obsidian"
)


class ObsidianLogger:

    def __init__(self, vault_path: str = "", folder: str = "자동매매") -> None:
        # vault_path 미지정(빈 문자열) → 프로젝트 내 기본 경로 자동 사용
        self._vault  = vault_path.strip() if vault_path and vault_path.strip() else _DEFAULT_VAULT
        self._folder = folder
        self._lock   = threading.Lock()
        logger.info(f"ObsidianLogger 초기화 | 볼트: {self._vault}")

    @property
    def enabled(self) -> bool:
        return True   # vault_path 유무와 무관하게 항상 활성

    @property
    def vault_path(self) -> str:
        return self._vault

    # ─── 거래 기록 ────────────────────────────────────────────────────────────

    def log_trade(self, scenario_id: str, trade, is_paper: bool = False) -> None:
        """매수/매도 1건을 일별 파일에 append."""
        try:
            path  = self._daily_file(scenario_id, is_paper)
            block = self._format_trade(trade, is_paper)
            with self._lock:
                with open(path, "a", encoding="utf-8") as f:
                    f.write(block)
        except Exception as e:
            logger.warning(f"Obsidian 기록 실패: {e}")

    # ─── 세션 시작/종료 기록 ─────────────────────────────────────────────────

    def log_session_start(self, scenarios: list[dict]) -> None:
        """
        시스템 시작 시 세션 파일 생성.
        scenarios: [{"scenario_id": ..., "is_paper": ..., "initial_balance": ...}, ...]
        """
        try:
            now      = datetime.now()
            filename = f"세션_{now.strftime('%Y-%m-%d_%H-%M-%S')}_시작.md"
            path     = os.path.join(self._vault, self._folder, "세션", filename)
            os.makedirs(os.path.dirname(path), exist_ok=True)

            lines = [
                f"# 🟢 세션 시작 — {now.strftime('%Y-%m-%d %H:%M:%S')}",
                "",
                "## 실행 시나리오",
                "",
                "| 모드 | 시나리오 | 초기자금 |",
                "|------|----------|----------|",
            ]
            for s in scenarios:
                mode = "🔵 가상" if s.get("is_paper") else "🟠 실제"
                bal  = s.get("initial_balance", 0)
                sid  = s.get("scenario_id", "?")
                lines.append(f"| {mode} | {sid} | {bal:,.0f}원 |")

            lines += ["", "---", ""]
            with self._lock:
                with open(path, "w", encoding="utf-8") as f:
                    f.write("\n".join(lines))
            logger.info(f"Obsidian 세션 시작 기록: {path}")
        except Exception as e:
            logger.warning(f"Obsidian 세션 시작 기록 실패: {e}")

    def log_session_end(self, summaries: list[dict]) -> None:
        """
        시스템 종료 시 세션 종료 파일 생성.
        summaries: get_all_summaries() 결과 (is_paper, scenario_id, total_pnl 등)
        """
        try:
            now      = datetime.now()
            filename = f"세션_{now.strftime('%Y-%m-%d_%H-%M-%S')}_종료.md"
            path     = os.path.join(self._vault, self._folder, "세션", filename)
            os.makedirs(os.path.dirname(path), exist_ok=True)

            lines = [
                f"# 🔴 세션 종료 — {now.strftime('%Y-%m-%d %H:%M:%S')}",
                "",
                "## 최종 결과",
                "",
                "| 모드 | 시나리오 | 초기자금 | 최종자산 | 손익 | 수익률 | 거래수 | 승률 |",
                "|------|----------|----------|----------|------|--------|--------|------|",
            ]
            for s in summaries:
                mode = "🔵 가상" if s.get("is_paper") else "🟠 실제"
                sign = "+" if s.get("total_pnl", 0) >= 0 else ""
                lines.append(
                    f"| {mode} "
                    f"| {s.get('scenario_id', '?')} "
                    f"| {s.get('initial_balance', 0):,.0f} "
                    f"| {s.get('current_equity', 0):,.0f} "
                    f"| {sign}{s.get('total_pnl', 0):,.0f} "
                    f"| {s.get('total_pnl_pct', 0):+.2f}% "
                    f"| {s.get('total_trades', 0)} "
                    f"| {s.get('win_rate', 0):.1f}% |"
                )

            lines += ["", "---", ""]
            with self._lock:
                with open(path, "w", encoding="utf-8") as f:
                    f.write("\n".join(lines))
            logger.info(f"Obsidian 세션 종료 기록: {path}")
        except Exception as e:
            logger.warning(f"Obsidian 세션 종료 기록 실패: {e}")

    # ─── 신호 진단 기록 ──────────────────────────────────────────────────────

    def log_diagnostic(
        self, scenario_id: str, ticker: str, reason: str, is_paper: bool = True
    ) -> None:
        """
        매수 신호 차단 이유를 시나리오별 진단 파일에 기록.
        동일 (scenario_id, ticker, reason) 조합은 스킵하지 않음 — 분석용 원시 로그.
        """
        try:
            now      = datetime.now()
            path     = os.path.join(
                self._vault, self._folder, "진단",
                f"{scenario_id}_진단.md"
            )
            os.makedirs(os.path.dirname(path), exist_ok=True)

            # 파일이 없으면 헤더 생성
            if not os.path.exists(path):
                with open(path, "w", encoding="utf-8") as f:
                    f.write(
                        f"# 📊 신호 진단 — {scenario_id}\n\n"
                        f"> 매수 신호가 차단된 이유 원시 로그입니다.\n\n"
                    )

            line = (
                f"- `{now.strftime('%H:%M:%S')}` "
                f"{'🔵' if is_paper else '🟠'} "
                f"**{ticker}** → `{reason}`\n"
            )
            with self._lock:
                with open(path, "a", encoding="utf-8") as f:
                    f.write(line)
        except Exception as e:
            logger.debug(f"Obsidian 진단 기록 실패: {e}")

    # ─── 일별 종합 리포트 ─────────────────────────────────────────────────────

    def log_daily_report(
        self,
        scenario_id: str,
        summary: dict,
        trade_history: list,   # PaperTrade 객체 또는 dict 리스트 (혼용 가능)
        is_paper: bool = True,
    ) -> None:
        """
        하루 거래 완료 시 일별 종합 리포트 저장.
        Path: {vault}/자동매매/일보/{date}_{scenario_id}_{가상|실제}.md

        trade_history 는 PaperTrade 또는 dict 모두 허용.
        오늘 날짜 기준으로 필터링하여 기록.
        """
        try:
            now    = datetime.now()
            mode   = "가상" if is_paper else "실제"
            date_s = now.strftime("%Y-%m-%d")
            path   = os.path.join(
                self._vault, self._folder, "일보",
                f"{date_s}_{scenario_id}_{mode}.md",
            )
            os.makedirs(os.path.dirname(path), exist_ok=True)

            # ── 공용 getter (PaperTrade / dict 모두 지원) ──
            def _g(t, key, default=""):
                if isinstance(t, dict):
                    return t.get(key, default)
                return getattr(t, key, default)

            # ── 오늘 날짜 기준 필터링 ──
            today = now.date()
            today_trades = []
            for t in trade_history:
                ts = _g(t, "timestamp")
                t_date = ts.date() if hasattr(ts, "date") else None
                if t_date is None:
                    try:
                        t_date = datetime.fromisoformat(str(ts)).date()
                    except Exception:
                        pass
                if t_date == today or t_date is None:
                    today_trades.append(t)

            # ── 헤더 ──
            mode_emoji = "🔵 가상거래" if is_paper else "🟠 실제거래"
            total_pnl  = summary.get("total_pnl", 0)
            pnl_emoji  = "🟢" if total_pnl >= 0 else "🔴"
            sign       = "+" if total_pnl >= 0 else ""

            lines = [
                f"# 📅 일별 거래 리포트 — {scenario_id} — {date_s}",
                "",
                f"> {mode_emoji}  |  생성: {now.strftime('%Y-%m-%d %H:%M:%S')}",
                "",
                "## 📊 최종 요약",
                "",
                "| 초기자금 | 최종자산 | 손익 | 수익률 | 총 거래 | 매도 | 승률 |",
                "|:-------:|:-------:|:---:|:-----:|:------:|:---:|:---:|",
                (
                    f"| {summary.get('initial_balance', 0):,.0f}원 "
                    f"| {summary.get('current_equity', 0):,.0f}원 "
                    f"| {pnl_emoji} {sign}{total_pnl:,.0f}원 "
                    f"| {summary.get('total_pnl_pct', 0):+.2f}% "
                    f"| {summary.get('total_trades', 0)} "
                    f"| {summary.get('sell_count', 0)} "
                    f"| {summary.get('win_rate', 0):.1f}% |"
                ),
                "",
            ]

            # ── 오늘 거래 내역 테이블 ──
            if today_trades:
                lines += [
                    f"## 🔄 오늘 거래 내역  ({len(today_trades)}건)",
                    "",
                    "| # | 시간 | 종목 | 구분 | 체결가 | 거래금액 | 손익 | 이유 |",
                    "|:-:|:----|:----|:----|------:|-------:|:----|:----|",
                ]
                for i, t in enumerate(today_trades, 1):
                    action     = _g(t, "action", "?")
                    act_emoji  = "📈 매수" if action == "BUY" else "📉 매도"
                    ts         = _g(t, "timestamp")
                    ts_str     = (
                        ts.strftime("%H:%M:%S")
                        if hasattr(ts, "strftime")
                        else str(ts)[11:19]
                    )
                    price      = _g(t, "price",      0)
                    amount_krw = _g(t, "amount_krw", 0)
                    pnl_v      = _g(t, "pnl",        0)
                    pnl_pct_v  = _g(t, "pnl_pct",    0)
                    reason_v   = _g(t, "reason",      "")
                    ticker_v   = _g(t, "ticker",      "")

                    pnl_str = ""
                    if action == "SELL":
                        pe      = "🟢" if pnl_v >= 0 else "🔴"
                        pnl_str = f"{pe} `{pnl_v:+,.0f}원` ({pnl_pct_v:+.2f}%)"

                    lines.append(
                        f"| {i} | `{ts_str}` | {ticker_v} | {act_emoji} "
                        f"| {price:,.0f} | {amount_krw:,.0f}원 "
                        f"| {pnl_str} | {reason_v} |"
                    )
                lines.append("")
            else:
                lines += [
                    "## 📭 오늘 거래 없음",
                    "",
                    "> 이 세션에서 거래가 발생하지 않았습니다.",
                    "",
                ]

            lines += ["---", ""]

            with self._lock:
                with open(path, "w", encoding="utf-8") as f:
                    f.write("\n".join(lines))
            logger.info(f"Obsidian 일보 저장: {path}")
        except Exception as e:
            logger.warning(f"Obsidian 일보 실패: {e}")

    # ─── 요약 기록 ───────────────────────────────────────────────────────────

    def write_summary(self, summaries: list[dict]) -> None:
        """3시간 요약 파일 생성."""
        if not summaries:
            return
        try:
            now      = datetime.now()
            filename = f"{now.strftime('%Y-%m-%d_%H-%M')}.md"
            path     = os.path.join(self._vault, self._folder, "요약", filename)
            os.makedirs(os.path.dirname(path), exist_ok=True)

            lines = [
                f"# 거래 요약 — {now.strftime('%Y-%m-%d %H:%M')}",
                "",
                "| 모드 | 시나리오 | 초기자금 | 현재평가 | 손익 | 수익률 | 거래수 | 승률 |",
                "|------|----------|----------|----------|------|--------|--------|------|",
            ]
            for s in summaries:
                mode = "가상" if s.get("is_paper") else "실제"
                sign = "+" if s["total_pnl"] >= 0 else ""
                lines.append(
                    f"| {mode} "
                    f"| {s['scenario_id']} "
                    f"| {s['initial_balance']:,.0f} "
                    f"| {s['current_equity']:,.0f} "
                    f"| {sign}{s['total_pnl']:,.0f} "
                    f"| {s['total_pnl_pct']:+.2f}% "
                    f"| {s['total_trades']} "
                    f"| {s['win_rate']:.1f}% |"
                )

            lines += ["", "---", ""]
            with self._lock:
                with open(path, "w", encoding="utf-8") as f:
                    f.write("\n".join(lines))
            logger.info(f"Obsidian 요약 저장: {path}")
        except Exception as e:
            logger.warning(f"Obsidian 요약 실패: {e}")

    # ─── 내부 유틸 ───────────────────────────────────────────────────────────

    def _daily_file(self, scenario_id: str, is_paper: bool) -> str:
        mode     = "가상" if is_paper else "실제"
        today    = date.today().strftime("%Y-%m-%d")
        dir_path = os.path.join(self._vault, self._folder, f"{mode}_{scenario_id}")
        os.makedirs(dir_path, exist_ok=True)
        path = os.path.join(dir_path, f"{today}.md")

        if not os.path.exists(path):
            mode_label = "🔵 가상거래" if is_paper else "🟠 실제거래"
            with open(path, "w", encoding="utf-8") as f:
                f.write(
                    f"# {mode_label} 거래 기록 — {scenario_id} — {today}\n\n"
                    f"> 자동 생성 파일. 수동 편집 가능.\n\n"
                )
        return path

    def _format_trade(self, trade, is_paper: bool) -> str:
        is_buy = trade.action == "BUY"
        emoji  = "📈 매수" if is_buy else "📉 매도"
        ts     = (
            trade.timestamp.strftime("%Y-%m-%d %H:%M:%S")
            if hasattr(trade.timestamp, "strftime")
            else str(trade.timestamp)
        )

        lines = [
            f"## {emoji} | {trade.ticker} | {ts}",
            "",
            f"- **시간**: `{ts}`",
            f"- **{'매수가' if is_buy else '매도가'}**: `{trade.price:,.0f}원`",
            f"- **수량**: `{trade.volume:.8f}`",
            f"- **거래금액**: `{trade.amount_krw:,.0f}원`",
            f"- **수수료**: `{trade.fee:,.0f}원`",
            f"- **이유**: {trade.reason}",
        ]

        if not is_buy:
            pnl_emoji = "🟢" if trade.pnl >= 0 else "🔴"
            lines += [
                f"- **손익**: {pnl_emoji} `{trade.pnl:+,.0f}원` ({trade.pnl_pct:+.2f}%)",
            ]

        lines += [
            f"- **잔고**: `{trade.balance_after:,.0f}원`",
            f"- **모드**: {'🔵 가상' if is_paper else '🟠 실제'}",
            "",
        ]

        # 지표 테이블
        if trade.indicators:
            lines += [
                "**지표 (거래 시점)**",
                "",
                "| 지표 | 값 |",
                "|------|-----|",
            ]
            for k, v in trade.indicators.items():
                if isinstance(v, float) and v > 1000:
                    lines.append(f"| {k} | `{v:,.0f}` |")
                elif isinstance(v, float):
                    lines.append(f"| {k} | `{v:.4f}` |")
                else:
                    lines.append(f"| {k} | `{v}` |")
            lines.append("")

        lines += ["---", ""]
        return "\n".join(lines)
