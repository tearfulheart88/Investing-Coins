import os
import csv
import json
import uuid
import logging
from dataclasses import dataclass, field, asdict
from datetime import datetime
from zoneinfo import ZoneInfo
from filelock import FileLock
import config
from core.telegram_notifier import send_real_trade_close_notification_async

# SQLite 연동 (선택적 — import 실패해도 JSONL 모드로 계속 동작)
try:
    from logging_.trade_db import TradeDB as _TradeDB
    _HAS_TRADE_DB = True
except Exception:
    _HAS_TRADE_DB = False

logger = logging.getLogger(__name__)

KST = ZoneInfo("Asia/Seoul")


def now_kst() -> str:
    return datetime.now(KST).isoformat()


@dataclass
class TradeRecord:
    ticker: str
    action: str                         # "BUY" / "SELL"
    price: float
    volume: float
    strategy_id: str
    scenario_id: str
    reason: str
    order_uuid: str
    krw_amount: int | None = None
    fee: float | None = None            # 수수료 (KRW)
    stop_loss_price: float | None = None
    pnl_krw: float | None = None
    pnl_pct: float | None = None
    total_equity: float | None = None
    metadata: dict = field(default_factory=dict)
    session_id: str | None = None       # 세션 ID (SessionManager가 자동 설정)
    record_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    timestamp: str = field(default_factory=now_kst)
    error: str | None = None


class TradeLogger:
    """
    거래 내역 기록.
    - trades.jsonl: JSONL(1행=1레코드) append-only → 대량 기록 시 성능 우수
    - trades_YYYY-MM.csv: 매도 완료 시 1행 기록
    - filelock으로 동시 쓰기 방지
    """

    def __init__(self) -> None:
        os.makedirs(os.path.dirname(config.TRADES_JSON_PATH), exist_ok=True)
        os.makedirs(os.path.dirname(config.REAL_PERFORMANCE_MD_PATH), exist_ok=True)
        # .json → .jsonl 경로 파생
        self._jsonl_path = config.TRADES_JSON_PATH.replace(".json", ".jsonl")
        self._jsonl_lock = FileLock(self._jsonl_path + ".lock")
        self._session_id: str | None = None   # 현재 세션 ID

        # SQLite DB (사용 가능하면 활성화)
        self._db: "_TradeDB | None" = None
        if _HAS_TRADE_DB:
            try:
                self._db = _TradeDB()
                self._update_realized_performance_report()
            except Exception as exc:
                logger.warning(f"[TradeLogger] SQLite 비활성화: {exc}")

    def set_session_id(self, session_id: str | None) -> None:
        """현재 세션 ID 설정. None이면 세션 태깅 비활성화."""
        self._session_id = session_id

    def log_trade(self, record: TradeRecord) -> None:
        """trades.jsonl에 거래 기록 추가, SELL이면 CSV에도 기록"""
        # 세션 ID 자동 주입
        if self._session_id and record.session_id is None:
            record.session_id = self._session_id
        self._append_jsonl(record)
        if record.action == "SELL":
            self._append_csv(record)
        # SQLite 병렬 기록 (실패해도 JSONL에는 이미 기록됨)
        if self._db is not None:
            inserted = self._db.insert_trade(record)
            if inserted and self._is_completed_sell(record):
                self._update_realized_performance_report()

        metadata = record.metadata if isinstance(record.metadata, dict) else {}
        if (
            self._is_completed_sell(record)
            and record.strategy_id not in ("exchange_sync", "unknown")
            and not metadata.get("backfilled")
        ):
            send_real_trade_close_notification_async(record)

        level = logging.WARNING if (
            record.action == "SELL" and record.pnl_pct is not None and record.pnl_pct < 0
        ) else logging.INFO

        pnl_str = ""
        if record.pnl_pct is not None:
            pnl_str = f" | pnl={record.pnl_krw:+,.0f}원({record.pnl_pct*100:+.2f}%)"

        fee_str = f" | fee={record.fee:,.0f}원" if record.fee is not None else ""

        logger.log(
            level,
            f"{record.action} | {record.ticker} | "
            f"price={record.price:,.0f} | vol={record.volume:.8f} | "
            f"reason={record.reason}{fee_str}{pnl_str}"
        )

    def log_signal(self, signal) -> None:
        """매수 신호를 DEBUG 레벨로 로깅 (파일에는 기록 안 함)"""
        logger.debug(
            f"SIGNAL | {signal.ticker} | should_buy={signal.should_buy} | "
            f"reason={signal.reason} | price={signal.current_price:,.0f}"
        )

    def log_equity_snapshot(self, equity: float, peak_equity: float) -> None:
        """1시간마다 포트폴리오 평가금액 스냅샷"""
        drawdown = (peak_equity - equity) / peak_equity * 100 if peak_equity > 0 else 0
        logger.info(
            f"EQUITY SNAPSHOT | 현재={equity:,.0f}원 | "
            f"peak={peak_equity:,.0f}원 | drawdown={drawdown:.2f}%"
        )

    # ─── JSONL (1행=1레코드, append-only) ────────────────────────────────────

    def _append_jsonl(self, record: TradeRecord) -> None:
        """JSONL 파일에 레코드 1행 추가 (읽기/재쓰기 불필요)"""
        record_dict = asdict(record)
        with self._jsonl_lock:
            with open(self._jsonl_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(record_dict, ensure_ascii=False) + "\n")

    # ─── CSV ─────────────────────────────────────────────────────────────────

    _CSV_FIELDS = [
        "record_id", "ticker", "strategy_id", "scenario_id",
        "timestamp", "action", "price", "volume",
        "krw_amount", "fee", "pnl_krw", "pnl_pct", "reason",
        "stop_loss_price", "total_equity", "error",
    ]

    def _append_csv(self, record: TradeRecord) -> None:
        """월별 CSV 파일에 행 추가 (SELL만)"""
        month_str = datetime.now(KST).strftime("%Y-%m")
        csv_dir = os.path.dirname(config.TRADES_JSON_PATH)
        csv_path = os.path.join(csv_dir, f"trades_{month_str}.csv")

        record_dict = asdict(record)
        row = {k: record_dict.get(k, "") for k in self._CSV_FIELDS}

        write_header = not os.path.exists(csv_path)
        with open(csv_path, "a", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=self._CSV_FIELDS)
            if write_header:
                writer.writeheader()
            writer.writerow(row)

    def _is_completed_sell(self, record: TradeRecord) -> bool:
        return (
            record.action == "SELL"
            and record.pnl_krw is not None
            and record.order_uuid != "FAILED"
        )

    def _query_completed_sell_rows(self, exclude_external: bool = False) -> list[dict]:
        if self._db is None:
            return []

        where_clauses = [
            "action = 'SELL'",
            "pnl_krw IS NOT NULL",
            "(order_uuid IS NULL OR order_uuid <> 'FAILED')",
        ]
        if exclude_external:
            where_clauses.append(
                "COALESCE(strategy_id, '') NOT IN ('exchange_sync', 'unknown')"
            )

        sql = f"""
            SELECT
                timestamp,
                ticker,
                strategy_id,
                scenario_id,
                session_id,
                reason,
                price,
                volume,
                fee,
                pnl_krw,
                pnl_pct
            FROM trades
            WHERE {' AND '.join(where_clauses)}
            ORDER BY timestamp ASC
        """
        return self._db.query(sql)

    def _query_recent_completed_sells(self, limit: int = 20) -> list[dict]:
        if self._db is None:
            return []

        sql = """
            SELECT
                timestamp,
                ticker,
                strategy_id,
                scenario_id,
                session_id,
                reason,
                pnl_krw,
                pnl_pct
            FROM trades
            WHERE action = 'SELL'
              AND pnl_krw IS NOT NULL
              AND (order_uuid IS NULL OR order_uuid <> 'FAILED')
            ORDER BY timestamp DESC
            LIMIT ?
        """
        return self._db.query(sql, (limit,))

    def _build_realized_summary(self, rows: list[dict]) -> dict:
        total_pnl = 0.0
        total_cost = 0.0
        pnl_pct_values: list[float] = []
        win_count = 0

        for row in rows:
            pnl_krw = float(row.get("pnl_krw") or 0.0)
            price = float(row.get("price") or 0.0)
            volume = float(row.get("volume") or 0.0)
            fee = float(row.get("fee") or 0.0)
            pnl_pct = row.get("pnl_pct")

            total_pnl += pnl_krw
            if pnl_krw > 0:
                win_count += 1

            estimated_cost = (price * volume) - fee - pnl_krw
            if estimated_cost > 0:
                total_cost += estimated_cost

            if pnl_pct is not None:
                pnl_pct_values.append(float(pnl_pct) * 100.0)

        sell_count = len(rows)
        weighted_return_pct = (total_pnl / total_cost * 100.0) if total_cost > 0 else 0.0

        return {
            "sell_count": sell_count,
            "win_count": win_count,
            "win_rate_pct": (win_count / sell_count * 100.0) if sell_count else 0.0,
            "total_pnl_krw": total_pnl,
            "weighted_return_pct": weighted_return_pct,
            "avg_pnl_pct": (
                sum(pnl_pct_values) / len(pnl_pct_values) if pnl_pct_values else 0.0
            ),
            "best_pnl_pct": max(pnl_pct_values) if pnl_pct_values else 0.0,
            "worst_pnl_pct": min(pnl_pct_values) if pnl_pct_values else 0.0,
            "first_closed_at": rows[0]["timestamp"] if rows else "",
            "last_closed_at": rows[-1]["timestamp"] if rows else "",
        }

    @staticmethod
    def _format_krw(value: float) -> str:
        return f"{value:,.0f}원"

    @staticmethod
    def _format_pct(value: float) -> str:
        return f"{value:+.3f}%"

    def _format_summary_block(self, summary: dict) -> list[str]:
        if summary["sell_count"] == 0:
            return ["- 완료된 매도 기록 없음"]

        return [
            f"- 완료 매도 수: {summary['sell_count']}건",
            f"- 승률: {summary['win_rate_pct']:.1f}% ({summary['win_count']}승)",
            f"- 누적 실현 손익: {self._format_krw(summary['total_pnl_krw'])}",
            f"- 가중 실현 수익률: {self._format_pct(summary['weighted_return_pct'])}",
            f"- 평균 손익률: {self._format_pct(summary['avg_pnl_pct'])}",
            f"- 최고/최저 손익률: {self._format_pct(summary['best_pnl_pct'])} / {self._format_pct(summary['worst_pnl_pct'])}",
            f"- 집계 구간: {summary['first_closed_at']} ~ {summary['last_closed_at']}",
        ]

    def _format_recent_rows(self, rows: list[dict]) -> list[str]:
        lines = [
            "| closed_at | ticker | scenario | strategy | source | reason | pnl_krw | pnl_pct |",
            "|---|---|---|---|---|---|---:|---:|",
        ]
        if not rows:
            lines.append("| - | - | - | - | - | - | 0원 | 0.000% |")
            return lines

        for row in rows:
            strategy_id = row.get("strategy_id") or "-"
            source = "bot" if strategy_id not in ("exchange_sync", "unknown") else "external/legacy"
            pnl_krw = float(row.get("pnl_krw") or 0.0)
            pnl_pct = float(row.get("pnl_pct") or 0.0) * 100.0
            lines.append(
                "| "
                f"{row.get('timestamp', '-')}"
                f" | {row.get('ticker', '-')}"
                f" | {row.get('scenario_id') or '-'}"
                f" | {strategy_id}"
                f" | {source}"
                f" | {row.get('reason') or '-'}"
                f" | {self._format_krw(pnl_krw)}"
                f" | {self._format_pct(pnl_pct)} |"
            )
        return lines

    def _format_realized_performance_markdown(
        self,
        all_summary: dict,
        bot_summary: dict,
        recent_rows: list[dict],
    ) -> str:
        lines = [
            "# Realized Performance",
            "",
            f"- Updated at: {now_kst()}",
            "- Scope: fully completed real-trading SELL records only",
            "- Note: `exchange_sync` and `unknown` are shown in total performance but excluded from bot-only performance",
            "",
            "## All Completed Sells",
            *self._format_summary_block(all_summary),
            "",
            "## Bot-Only Completed Sells",
            *self._format_summary_block(bot_summary),
            "",
            "## Recent Completed Sells",
            *self._format_recent_rows(recent_rows),
            "",
        ]
        return "\n".join(lines)

    def _update_realized_performance_report(self) -> None:
        if self._db is None:
            return

        try:
            all_rows = self._query_completed_sell_rows(exclude_external=False)
            bot_rows = self._query_completed_sell_rows(exclude_external=True)
            recent_rows = self._query_recent_completed_sells(limit=20)
            markdown = self._format_realized_performance_markdown(
                all_summary=self._build_realized_summary(all_rows),
                bot_summary=self._build_realized_summary(bot_rows),
                recent_rows=recent_rows,
            )
            with open(config.REAL_PERFORMANCE_MD_PATH, "w", encoding="utf-8") as handle:
                handle.write(markdown)
        except Exception as exc:
            logger.warning(f"[TradeLogger] 실현 성과 리포트 갱신 실패: {exc}")
