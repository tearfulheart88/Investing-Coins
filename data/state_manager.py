import os
import json
import threading
import logging
from dataclasses import dataclass, asdict
from datetime import datetime
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)
KST = ZoneInfo("Asia/Seoul")


@dataclass
class Position:
    ticker: str
    volume: float               # 보유 코인 수량
    buy_price: float            # 평균 매수가 (KRW/코인)
    buy_time: str               # ISO-8601 KST 타임스탬프
    krw_spent: int              # 실투자 원화
    order_uuid: str             # 업비트 주문 UUID
    stop_loss_price: float      # 손절 기준가 = buy_price * (1 - stop_loss_pct)
    strategy_id: str            # 어떤 전략으로 매수했는지
    scenario_id: str            # 어떤 시나리오로 매수했는지
    side: str = "LONG"          # "LONG" | "SHORT" (현물=LONG)
    leverage: int = 1           # 현물=1, 선물=2~125
    liquidation_price: float = 0.0  # 0=현물(청산 없음)

    def unrealized_pnl_pct(self, current_price: float) -> float:
        """미실현 손익률"""
        if self.buy_price <= 0:
            return 0.0
        if self.side == "LONG":
            return (current_price - self.buy_price) / self.buy_price
        return (self.buy_price - current_price) / self.buy_price

    def unrealized_pnl_krw(self, current_price: float) -> float:
        """미실현 손익 (KRW)"""
        if self.side == "LONG":
            return (current_price * self.volume) - self.krw_spent
        return self.krw_spent - (current_price * self.volume)


class StateManager:
    """
    포지션 상태 영속성 관리.
    - positions.json에 저장/불러오기
    - 원자적 쓰기: .tmp 파일 경유 os.replace()
    - Thread-safe: threading.Lock 사용
    - 시작 시 업비트 실제 잔고와 대조 (reconcile)
    """

    def __init__(self, path: str) -> None:
        self._path = path
        self._lock = threading.Lock()
        self._positions: dict[str, Position] = {}
        self._peak_equity: float = 0.0
        os.makedirs(os.path.dirname(path), exist_ok=True)

    # ─── 로드 / 저장 ──────────────────────────────────────────────────────────

    def load(self) -> None:
        """파일에서 포지션 로드. 파일 없거나 손상 시 빈 상태로 시작."""
        if not os.path.exists(self._path):
            logger.info("positions.json 없음, 빈 상태로 시작")
            return

        try:
            with open(self._path, "r", encoding="utf-8") as f:
                data = json.load(f)

            meta = data.get("meta", {})
            self._peak_equity = float(meta.get("peak_equity", 0.0))

            positions_data = data.get("positions", {})
            with self._lock:
                self._positions = {}
                for ticker, pos_dict in positions_data.items():
                    # 신규 필드 기본값 폴백 (기존 JSON에 없을 수 있음)
                    pos_dict.setdefault("side", "LONG")
                    pos_dict.setdefault("leverage", 1)
                    pos_dict.setdefault("liquidation_price", 0.0)
                    self._positions[ticker] = Position(**pos_dict)
            logger.info(f"포지션 로드 완료: {list(self._positions.keys())}, peak_equity={self._peak_equity:,.0f}")

        except Exception as e:
            logger.critical(f"positions.json 로드 실패, 빈 상태로 시작: {e}")
            self._positions = {}
            self._peak_equity = 0.0

    def save(self) -> None:
        """원자적 쓰기로 포지션 저장."""
        tmp_path = self._path + ".tmp"
        try:
            with self._lock:
                data = {
                    "meta": {
                        "peak_equity": self._peak_equity,
                    },
                    "positions": {
                        ticker: asdict(pos)
                        for ticker, pos in self._positions.items()
                    },
                }

            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)

            os.replace(tmp_path, self._path)

        except Exception as e:
            logger.critical(f"포지션 저장 실패 (인메모리 유지): {e}")
            if os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except Exception:
                    pass

    # ─── CRUD ─────────────────────────────────────────────────────────────────

    def add_position(self, position: Position) -> None:
        with self._lock:
            self._positions[position.ticker] = position

    def remove_position(self, ticker: str) -> Position | None:
        with self._lock:
            return self._positions.pop(ticker, None)

    def get_position(self, ticker: str) -> Position | None:
        with self._lock:
            return self._positions.get(ticker)

    def has_position(self, ticker: str) -> bool:
        with self._lock:
            return ticker in self._positions

    def all_positions(self) -> list[Position]:
        with self._lock:
            return list(self._positions.values())

    def update_position_entry(
        self,
        ticker: str,
        new_buy_price: float,
        new_stop_loss: float,
    ) -> bool:
        """
        재진입(Re-entry): 포지션의 기준 매수가와 손절가를 갱신합니다.
        수량·투자원금(krw_spent)은 그대로 유지됩니다.

        Returns
        -------
        bool
            포지션이 존재해 갱신에 성공하면 True, 없으면 False.
        """
        with self._lock:
            pos = self._positions.get(ticker)
            if pos is None:
                return False
            pos.buy_price       = new_buy_price
            pos.stop_loss_price = new_stop_loss
        self.save()
        logger.info(
            f"[StateManager] 재진입 갱신 | {ticker} | "
            f"buy_price={new_buy_price:,.0f} | stop_loss={new_stop_loss:,.0f}"
        )
        return True

    # ─── Peak Equity ──────────────────────────────────────────────────────────

    @property
    def peak_equity(self) -> float:
        return self._peak_equity

    def update_peak_equity(self, current_equity: float) -> None:
        if current_equity > self._peak_equity:
            self._peak_equity = current_equity
            logger.info(f"Peak equity 갱신: {current_equity:,.0f}원")

    # ─── 재시작 시 잔고 대조 ──────────────────────────────────────────────────

    def reconcile_with_exchange(self, client) -> None:
        """
        저장된 포지션과 실제 업비트 잔고를 대조.
        잔고가 0인 포지션은 stale → 제거 후 WARNING 로그.
        """
        stale = []
        with self._lock:
            tickers = list(self._positions.keys())

        for ticker in tickers:
            try:
                balance = client.get_balance(ticker)
                if balance <= 0:
                    stale.append(ticker)
                    logger.warning(f"스테일 포지션 제거 (잔고 없음): {ticker}")
            except Exception as e:
                logger.warning(f"포지션 대조 실패, 유지: {ticker} - {e}")

        for ticker in stale:
            self.remove_position(ticker)

        if stale:
            self.save()
            logger.info(f"스테일 포지션 {len(stale)}개 제거 완료")
        else:
            logger.info("포지션 대조 완료 - 이상 없음")

    def sync_from_exchange(
        self,
        client,
        tickers: list[str] | None = None,
        default_scenario_id: str = "exchange_sync",
    ) -> list[str]:
        """
        업비트 실제 보유 잔고 → 내부 포지션 자동 등록.
        positions.json에 없는 종목을 거래소에서 직접 가져와 포지션으로 등록.
        이미 추적 중인 종목은 건너뜀.

        tickers=None이면 계좌의 전체 보유 코인을 모두 동기화.
        tickers 지정 시 해당 목록에 포함된 종목만 동기화.
        반환값: 새로 등록된 ticker 목록
        """
        import config as _cfg

        try:
            balances = client.get_balances()
        except Exception as e:
            logger.warning(f"거래소 포지션 동기화 실패 (잔고 조회 오류): {e}")
            return []

        # currency → {balance, avg_buy_price} 매핑
        bal_map: dict[str, dict] = {}
        for b in balances:
            currency = b.get("currency", "")
            amount   = float(b.get("balance", 0) or 0)
            if currency != "KRW" and amount > 0:
                bal_map[currency] = {
                    "balance":       amount,
                    "avg_buy_price": float(b.get("avg_buy_price", 0) or 0),
                }

        added: list[str] = []
        now_str = datetime.now(KST).isoformat()

        # 블랙리스트 (스테이블코인, 신규 상장 등 제외)
        blacklist: set[str] = set(getattr(_cfg, "TICKER_BLACKLIST", []))

        # tickers=None이면 계좌 전체 코인 대상, 아니면 지정 목록으로 필터
        # 두 경우 모두 블랙리스트에 있는 종목은 제외
        candidates: list[str] = (
            [f"KRW-{cur}" for cur in bal_map if f"KRW-{cur}" not in blacklist]
            if tickers is None
            else [t for t in tickers if t not in blacklist]
        )

        for ticker in candidates:
            currency = ticker.replace("KRW-", "")
            if currency not in bal_map:
                continue
            if self.has_position(ticker):
                continue  # 이미 추적 중

            data      = bal_map[currency]
            avg_price = data["avg_buy_price"]
            volume    = data["balance"]

            if avg_price <= 0 or volume <= 0:
                logger.warning(f"거래소 동기화 건너뜀 (평균가 0 또는 수량 0): {ticker}")
                continue

            sl_pct          = _cfg.STOP_LOSS_PCT
            stop_loss_price = avg_price * (1 - sl_pct)

            pos = Position(
                ticker          = ticker,
                volume          = volume,
                buy_price       = avg_price,
                buy_time        = now_str,
                krw_spent       = int(avg_price * volume),
                order_uuid      = "EXCHANGE_SYNC",
                stop_loss_price = stop_loss_price,
                strategy_id     = "exchange_sync",
                scenario_id     = default_scenario_id,
            )
            self.add_position(pos)
            added.append(ticker)
            logger.info(
                f"거래소 보유 포지션 등록: {ticker} | "
                f"volume={volume:.8f} | avg_price={avg_price:,.0f}원 | "
                f"scenario={default_scenario_id}"
            )

        if added:
            self.save()
            logger.info(f"거래소 동기화 완료: {len(added)}개 추가 {added}")
        else:
            logger.info("거래소 동기화: 추가할 미추적 포지션 없음")

        return added
