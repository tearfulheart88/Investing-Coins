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
        # .json → .jsonl 경로 파생
        self._jsonl_path = config.TRADES_JSON_PATH.replace(".json", ".jsonl")
        self._jsonl_lock = FileLock(self._jsonl_path + ".lock")
        self._session_id: str | None = None   # 현재 세션 ID

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
