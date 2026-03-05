"""
trade_db.py — SQLite 거래 데이터베이스

JSONL과 병렬로 거래 기록을 SQLite에 저장합니다.
Claude Code의 /trades 스킬에서 SQL 쿼리로 빠르게 분석할 수 있습니다.

DB 경로: logs/trades/trades.db
테이블:  trades (record_id PK)
"""

import sqlite3
import os
import json
import logging
from dataclasses import asdict
from datetime import datetime
from zoneinfo import ZoneInfo
from threading import Lock

import config

logger = logging.getLogger(__name__)

KST = ZoneInfo("Asia/Seoul")

# ─── DDL ─────────────────────────────────────────────────────────────────────

_CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS trades (
    record_id        TEXT PRIMARY KEY,
    ticker           TEXT NOT NULL,
    action           TEXT NOT NULL,
    price            REAL NOT NULL,
    volume           REAL NOT NULL,
    strategy_id      TEXT,
    scenario_id      TEXT,
    reason           TEXT,
    order_uuid       TEXT,
    krw_amount       INTEGER,
    fee              REAL,
    stop_loss_price  REAL,
    pnl_krw          REAL,
    pnl_pct          REAL,
    total_equity     REAL,
    session_id       TEXT,
    timestamp        TEXT NOT NULL,
    error            TEXT,
    metadata         TEXT    -- JSON 직렬화
);
"""

_CREATE_INDEXES_SQL = [
    "CREATE INDEX IF NOT EXISTS idx_trades_ticker     ON trades(ticker);",
    "CREATE INDEX IF NOT EXISTS idx_trades_scenario   ON trades(scenario_id);",
    "CREATE INDEX IF NOT EXISTS idx_trades_action     ON trades(action);",
    "CREATE INDEX IF NOT EXISTS idx_trades_timestamp  ON trades(timestamp);",
    "CREATE INDEX IF NOT EXISTS idx_trades_session    ON trades(session_id);",
    "CREATE INDEX IF NOT EXISTS idx_trades_pnl        ON trades(pnl_pct);",
]


# ─── TradeDB ─────────────────────────────────────────────────────────────────

class TradeDB:
    """
    SQLite 기반 거래 데이터베이스.
    thread-safe (Lock + WAL 모드).
    """

    def __init__(self, db_path: str | None = None) -> None:
        if db_path is None:
            trades_dir = os.path.dirname(config.TRADES_JSON_PATH)
            db_path = os.path.join(trades_dir, "trades.db")

        self._db_path = db_path
        self._lock = Lock()
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        self._init_db()
        logger.info(f"[TradeDB] 초기화 완료: {db_path}")

    # ─── 초기화 ──────────────────────────────────────────────────────────────

    def _init_db(self) -> None:
        with self._connect() as con:
            con.execute(_CREATE_TABLE_SQL)
            for idx_sql in _CREATE_INDEXES_SQL:
                con.execute(idx_sql)
            con.commit()

    def _connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(self._db_path, check_same_thread=False, timeout=10)
        con.execute("PRAGMA journal_mode=WAL")   # 동시 읽기 성능 향상
        con.execute("PRAGMA synchronous=NORMAL") # 성능 ↑ (안전성 유지)
        con.row_factory = sqlite3.Row
        return con

    # ─── 쓰기 ────────────────────────────────────────────────────────────────

    def insert_trade(self, record) -> bool:
        """
        TradeRecord 인스턴스를 trades 테이블에 INSERT OR IGNORE.
        이미 동일한 record_id가 있으면 무시 (멱등).
        """
        try:
            d = asdict(record)
            with self._lock:
                with self._connect() as con:
                    con.execute(
                        """
                        INSERT OR IGNORE INTO trades (
                            record_id, ticker, action, price, volume,
                            strategy_id, scenario_id, reason, order_uuid,
                            krw_amount, fee, stop_loss_price,
                            pnl_krw, pnl_pct, total_equity,
                            session_id, timestamp, error, metadata
                        ) VALUES (
                            :record_id, :ticker, :action, :price, :volume,
                            :strategy_id, :scenario_id, :reason, :order_uuid,
                            :krw_amount, :fee, :stop_loss_price,
                            :pnl_krw, :pnl_pct, :total_equity,
                            :session_id, :timestamp, :error, :metadata
                        )
                        """,
                        {**d, "metadata": json.dumps(d.get("metadata", {}), ensure_ascii=False)},
                    )
                    con.commit()
            return True
        except Exception as exc:
            logger.error(f"[TradeDB] INSERT 실패: {exc}")
            return False

    # ─── 읽기 ────────────────────────────────────────────────────────────────

    def query(self, sql: str, params: tuple = ()) -> list[dict]:
        """임의 SELECT SQL 실행 후 dict 리스트 반환."""
        try:
            with self._connect() as con:
                rows = con.execute(sql, params).fetchall()
            return [dict(r) for r in rows]
        except Exception as exc:
            logger.error(f"[TradeDB] QUERY 실패: {exc}")
            return []

    def recent_trades(
        self,
        limit: int = 30,
        action: str | None = None,
        ticker: str | None = None,
        scenario_id: str | None = None,
        session_id: str | None = None,
    ) -> list[dict]:
        """조건 기반 최근 거래 조회."""
        clauses, params = [], []
        if action:
            clauses.append("action = ?"); params.append(action)
        if ticker:
            clauses.append("ticker = ?"); params.append(ticker)
        if scenario_id:
            clauses.append("scenario_id = ?"); params.append(scenario_id)
        if session_id:
            clauses.append("session_id = ?"); params.append(session_id)

        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        sql = f"SELECT * FROM trades {where} ORDER BY timestamp DESC LIMIT ?"
        params.append(limit)
        return self.query(sql, tuple(params))

    def stats_by_scenario(self) -> list[dict]:
        """시나리오별 승률·평균손익 집계."""
        return self.query("""
            SELECT
                scenario_id,
                COUNT(*)                                                AS total_trades,
                SUM(CASE WHEN pnl_pct > 0 THEN 1 ELSE 0 END)          AS wins,
                ROUND(AVG(CASE WHEN pnl_pct > 0 THEN 1.0 ELSE 0 END) * 100, 1) AS win_rate_pct,
                ROUND(AVG(pnl_pct) * 100, 3)                           AS avg_pnl_pct,
                ROUND(MAX(pnl_pct) * 100, 3)                           AS best_pnl_pct,
                ROUND(MIN(pnl_pct) * 100, 3)                           AS worst_pnl_pct,
                ROUND(SUM(pnl_krw), 0)                                 AS total_pnl_krw
            FROM trades
            WHERE action = 'SELL'
            GROUP BY scenario_id
            ORDER BY total_pnl_krw DESC
        """)

    def stats_by_ticker(self) -> list[dict]:
        """종목별 수익 집계."""
        return self.query("""
            SELECT
                ticker,
                COUNT(*) AS sells,
                ROUND(AVG(CASE WHEN pnl_pct>0 THEN 1.0 ELSE 0 END)*100,1) AS win_rate_pct,
                ROUND(SUM(pnl_krw), 0) AS total_pnl_krw
            FROM trades
            WHERE action = 'SELL'
            GROUP BY ticker
            ORDER BY total_pnl_krw DESC
        """)

    def count(self) -> int:
        rows = self.query("SELECT COUNT(*) AS cnt FROM trades")
        return rows[0]["cnt"] if rows else 0

    # ─── 마이그레이션: JSONL → SQLite ────────────────────────────────────────

    def migrate_from_jsonl(self, jsonl_path: str | None = None) -> int:
        """
        기존 trades.jsonl 데이터를 SQLite로 일괄 임포트.
        이미 존재하는 record_id는 IGNORE (멱등).
        반환값: 신규 삽입 건수
        """
        if jsonl_path is None:
            jsonl_path = config.TRADES_JSON_PATH.replace(".json", ".jsonl")

        if not os.path.exists(jsonl_path):
            logger.warning(f"[TradeDB] JSONL 없음: {jsonl_path}")
            return 0

        inserted = 0
        try:
            with open(jsonl_path, encoding="utf-8") as f:
                lines = [l.strip() for l in f if l.strip()]

            with self._lock:
                with self._connect() as con:
                    for line in lines:
                        try:
                            d = json.loads(line)
                            con.execute(
                                """
                                INSERT OR IGNORE INTO trades (
                                    record_id, ticker, action, price, volume,
                                    strategy_id, scenario_id, reason, order_uuid,
                                    krw_amount, fee, stop_loss_price,
                                    pnl_krw, pnl_pct, total_equity,
                                    session_id, timestamp, error, metadata
                                ) VALUES (
                                    :record_id, :ticker, :action, :price, :volume,
                                    :strategy_id, :scenario_id, :reason, :order_uuid,
                                    :krw_amount, :fee, :stop_loss_price,
                                    :pnl_krw, :pnl_pct, :total_equity,
                                    :session_id, :timestamp, :error, :metadata
                                )
                                """,
                                {**d, "metadata": json.dumps(d.get("metadata", {}), ensure_ascii=False)},
                            )
                            if con.execute("SELECT changes()").fetchone()[0]:
                                inserted += 1
                        except Exception as exc:
                            logger.debug(f"[TradeDB] 행 스킵: {exc}")
                    con.commit()

            logger.info(f"[TradeDB] 마이그레이션 완료: {inserted}/{len(lines)} 건 삽입")
        except Exception as exc:
            logger.error(f"[TradeDB] 마이그레이션 실패: {exc}")

        return inserted


# ─── CLI 진입점 ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    import argparse

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    parser = argparse.ArgumentParser(description="거래 DB 관리 CLI")
    sub = parser.add_subparsers(dest="cmd")

    sub.add_parser("migrate", help="JSONL → SQLite 마이그레이션")
    sub.add_parser("stats",   help="시나리오별 통계")
    sub.add_parser("tickers", help="종목별 통계")

    qp = sub.add_parser("query", help="임의 SQL 실행")
    qp.add_argument("sql", nargs="?", default="SELECT * FROM trades ORDER BY timestamp DESC LIMIT 10")

    rp = sub.add_parser("recent", help="최근 거래 조회")
    rp.add_argument("--limit",    type=int, default=20)
    rp.add_argument("--action",   default=None)
    rp.add_argument("--ticker",   default=None)
    rp.add_argument("--scenario", default=None)
    rp.add_argument("--session",  default=None)

    args = parser.parse_args()
    db = TradeDB()

    if args.cmd == "migrate":
        n = db.migrate_from_jsonl()
        print(f"✅ {n}건 마이그레이션 완료  (총 DB: {db.count()}건)")

    elif args.cmd == "stats":
        rows = db.stats_by_scenario()
        if not rows:
            print("데이터 없음")
        else:
            print(f"{'시나리오':<25} {'거래':>5} {'승률':>7} {'평균PnL':>9} {'총손익':>12}")
            print("-" * 65)
            for r in rows:
                print(
                    f"{r['scenario_id']:<25} {r['total_trades']:>5} "
                    f"{r['win_rate_pct']:>6.1f}% {r['avg_pnl_pct']:>+8.3f}% "
                    f"{r['total_pnl_krw']:>+12,.0f}원"
                )

    elif args.cmd == "tickers":
        rows = db.stats_by_ticker()
        print(f"{'종목':<14} {'매도':>5} {'승률':>7} {'총손익':>12}")
        print("-" * 45)
        for r in rows:
            print(f"{r['ticker']:<14} {r['sells']:>5} {r['win_rate_pct']:>6.1f}% {r['total_pnl_krw']:>+12,.0f}원")

    elif args.cmd == "query":
        rows = db.query(args.sql)
        if not rows:
            print("결과 없음")
        else:
            keys = list(rows[0].keys())
            print("  ".join(f"{k:>12}" for k in keys))
            print("-" * (14 * len(keys)))
            for r in rows:
                print("  ".join(f"{str(r[k]):>12}" for k in keys))

    elif args.cmd == "recent":
        rows = db.recent_trades(
            limit=args.limit,
            action=args.action,
            ticker=args.ticker,
            scenario_id=args.scenario,
            session_id=args.session,
        )
        for r in rows:
            pnl = f"{r['pnl_pct']*100:+.2f}%" if r["pnl_pct"] else "  -  "
            print(f"[{r['timestamp'][:16]}] {r['action']:6} {r['ticker']:<12} {r['price']:>12,.0f}원  pnl={pnl}  {r['reason']}")

    else:
        parser.print_help()
