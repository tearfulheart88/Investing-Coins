"""
주문 생명주기 상태머신 — OrderStateMachine

■ 상태 전이:
  IDLE → ENTRY_PENDING → IN_POSITION → EXIT_PENDING → IDLE
                ↓ (취소/실패)          ↓ (취소/실패)
              IDLE                 IN_POSITION (재시도)

■ 각 상태:
  IDLE           : 포지션 없음. 매수 신호 대기.
  ENTRY_PENDING  : 매수 주문 접수됨. 체결 대기.
  IN_POSITION    : 포지션 보유. 매도 신호/손절/트레일링 감시.
  EXIT_PENDING   : 매도 주문 접수됨. 체결 대기.

■ 사용법:
  sm = OrderStateMachine()
  sm.request_entry(ticker, order_uuid, entry_price, sl_price)
  sm.confirm_entry(ticker, filled_qty, avg_price)
  sm.request_exit(ticker, order_uuid, reason)
  sm.confirm_exit(ticker)
  state = sm.get_state(ticker)
"""

import time
import logging
from enum import Enum
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


class OrderState(Enum):
    IDLE = "idle"
    ENTRY_PENDING = "entry_pending"
    IN_POSITION = "in_position"
    EXIT_PENDING = "exit_pending"


@dataclass
class OrderContext:
    """단일 종목의 주문 생명주기 컨텍스트."""
    ticker: str
    state: OrderState = OrderState.IDLE

    # 주문 정보
    order_uuid: str = ""
    identifier: str = ""          # 업비트 idempotency key

    # Entry
    entry_price: float = 0.0
    sl_price: float = 0.0
    filled_qty: float = 0.0
    filled_avg_price: float = 0.0

    # Exit
    exit_reason: str = ""
    exit_order_uuid: str = ""

    # 타임스탬프
    entry_request_ts: float = 0.0
    entry_confirm_ts: float = 0.0
    exit_request_ts: float = 0.0
    exit_confirm_ts: float = 0.0

    # 타임아웃 (초)
    entry_timeout_sec: float = 10.0   # 매수 미체결 타임아웃
    exit_timeout_sec: float = 10.0    # 매도 미체결 타임아웃


class OrderStateMachine:
    """
    주문 생명주기 상태머신.
    종목별 독립적 상태 관리.
    """

    def __init__(self, entry_timeout_sec: float = 10.0, exit_timeout_sec: float = 10.0) -> None:
        self._contexts: dict[str, OrderContext] = {}
        self._entry_timeout = entry_timeout_sec
        self._exit_timeout = exit_timeout_sec

    # ─── 상태 조회 ─────────────────────────────────────────────────────────────

    def get_state(self, ticker: str) -> OrderState:
        ctx = self._contexts.get(ticker)
        return ctx.state if ctx else OrderState.IDLE

    def get_context(self, ticker: str) -> OrderContext | None:
        return self._contexts.get(ticker)

    def is_idle(self, ticker: str) -> bool:
        return self.get_state(ticker) == OrderState.IDLE

    def is_in_position(self, ticker: str) -> bool:
        return self.get_state(ticker) == OrderState.IN_POSITION

    def has_pending_order(self, ticker: str) -> bool:
        state = self.get_state(ticker)
        return state in (OrderState.ENTRY_PENDING, OrderState.EXIT_PENDING)

    # ─── 상태 전이: 매수 ──────────────────────────────────────────────────────

    def request_entry(
        self,
        ticker: str,
        order_uuid: str,
        entry_price: float,
        sl_price: float,
        identifier: str = "",
    ) -> bool:
        """
        매수 주문 요청 → IDLE → ENTRY_PENDING.
        이미 다른 상태면 무시 (중복 주문 방지).
        """
        state = self.get_state(ticker)
        if state != OrderState.IDLE:
            logger.warning(
                f"[OrderSM] {ticker} 매수 요청 거부: "
                f"현재 상태={state.value} (IDLE 아님)"
            )
            return False

        ctx = OrderContext(
            ticker=ticker,
            state=OrderState.ENTRY_PENDING,
            order_uuid=order_uuid,
            identifier=identifier,
            entry_price=entry_price,
            sl_price=sl_price,
            entry_request_ts=time.time(),
            entry_timeout_sec=self._entry_timeout,
            exit_timeout_sec=self._exit_timeout,
        )
        self._contexts[ticker] = ctx

        logger.info(
            f"[OrderSM] {ticker} IDLE → ENTRY_PENDING | "
            f"uuid={order_uuid} price={entry_price:,.0f} sl={sl_price:,.0f}"
        )
        return True

    def confirm_entry(
        self,
        ticker: str,
        filled_qty: float,
        avg_price: float,
    ) -> bool:
        """
        매수 체결 확인 → ENTRY_PENDING → IN_POSITION.
        """
        ctx = self._contexts.get(ticker)
        if ctx is None or ctx.state != OrderState.ENTRY_PENDING:
            logger.warning(f"[OrderSM] {ticker} 매수 확인 무시: 상태 불일치")
            return False

        ctx.state = OrderState.IN_POSITION
        ctx.filled_qty = filled_qty
        ctx.filled_avg_price = avg_price
        ctx.entry_confirm_ts = time.time()

        logger.info(
            f"[OrderSM] {ticker} ENTRY_PENDING → IN_POSITION | "
            f"qty={filled_qty:.8f} avg={avg_price:,.0f}"
        )
        return True

    def cancel_entry(self, ticker: str, reason: str = "") -> bool:
        """
        매수 취소/실패 → ENTRY_PENDING → IDLE.
        """
        ctx = self._contexts.get(ticker)
        if ctx is None or ctx.state != OrderState.ENTRY_PENDING:
            return False

        logger.info(f"[OrderSM] {ticker} ENTRY_PENDING → IDLE (취소: {reason})")
        ctx.state = OrderState.IDLE
        self._contexts.pop(ticker, None)
        return True

    def sync_position(
        self,
        ticker: str,
        entry_price: float,
        sl_price: float,
        filled_qty: float,
        order_uuid: str = "",
        identifier: str = "",
    ) -> None:
        """
        이미 보유 중인 포지션을 상태머신에 동기화한다.

        - 재시작 후 positions.json 로드
        - 거래소 잔고 동기화로 새로 유입된 포지션 등록
        - 주문 상태 유실 후 매도/손절 복구
        """
        self._contexts[ticker] = OrderContext(
            ticker=ticker,
            state=OrderState.IN_POSITION,
            order_uuid=order_uuid,
            identifier=identifier,
            entry_price=entry_price,
            sl_price=sl_price,
            filled_qty=filled_qty,
            filled_avg_price=entry_price,
            entry_confirm_ts=time.time(),
            entry_timeout_sec=self._entry_timeout,
            exit_timeout_sec=self._exit_timeout,
        )
        logger.info(
            f"[OrderSM] {ticker} 상태 동기화 → IN_POSITION | "
            f"entry={entry_price:,.0f} qty={filled_qty:.8f} sl={sl_price:,.0f}"
        )

    # ─── 상태 전이: 매도 ──────────────────────────────────────────────────────

    def request_exit(
        self,
        ticker: str,
        order_uuid: str,
        reason: str,
        identifier: str = "",
    ) -> bool:
        """
        매도 주문 요청 → IN_POSITION → EXIT_PENDING.
        """
        ctx = self._contexts.get(ticker)
        if ctx is None or ctx.state != OrderState.IN_POSITION:
            logger.warning(
                f"[OrderSM] {ticker} 매도 요청 거부: "
                f"현재 상태={ctx.state.value if ctx else 'None'}"
            )
            return False

        ctx.state = OrderState.EXIT_PENDING
        ctx.exit_order_uuid = order_uuid
        ctx.exit_reason = reason
        ctx.exit_request_ts = time.time()
        ctx.identifier = identifier

        logger.info(
            f"[OrderSM] {ticker} IN_POSITION → EXIT_PENDING | "
            f"reason={reason} uuid={order_uuid}"
        )
        return True

    def confirm_exit(self, ticker: str) -> bool:
        """
        매도 체결 확인 → EXIT_PENDING → IDLE (컨텍스트 제거).
        """
        ctx = self._contexts.get(ticker)
        if ctx is None or ctx.state != OrderState.EXIT_PENDING:
            logger.warning(f"[OrderSM] {ticker} 매도 확인 무시: 상태 불일치")
            return False

        ctx.exit_confirm_ts = time.time()
        logger.info(f"[OrderSM] {ticker} EXIT_PENDING → IDLE (청산 완료)")
        self._contexts.pop(ticker, None)
        return True

    def cancel_exit(self, ticker: str, reason: str = "") -> bool:
        """
        매도 취소/실패 → EXIT_PENDING → IN_POSITION (재시도 가능).
        """
        ctx = self._contexts.get(ticker)
        if ctx is None or ctx.state != OrderState.EXIT_PENDING:
            return False

        logger.info(
            f"[OrderSM] {ticker} EXIT_PENDING → IN_POSITION "
            f"(매도 취소: {reason}, 재시도 가능)"
        )
        ctx.state = OrderState.IN_POSITION
        ctx.exit_order_uuid = ""
        ctx.exit_reason = ""
        return True

    # ─── 타임아웃 체크 ─────────────────────────────────────────────────────────

    def check_timeouts(self) -> list[tuple[str, OrderState]]:
        """
        타임아웃된 pending 주문 목록 반환.
        호출부에서 cancel_entry / cancel_exit 처리.

        Returns: [(ticker, state), ...]
        """
        now = time.time()
        expired: list[tuple[str, OrderState]] = []

        for ticker, ctx in list(self._contexts.items()):
            if ctx.state == OrderState.ENTRY_PENDING:
                if now - ctx.entry_request_ts > ctx.entry_timeout_sec:
                    expired.append((ticker, ctx.state))

            elif ctx.state == OrderState.EXIT_PENDING:
                if now - ctx.exit_request_ts > ctx.exit_timeout_sec:
                    expired.append((ticker, ctx.state))

        return expired

    # ─── 유틸 ──────────────────────────────────────────────────────────────────

    def active_positions(self) -> list[str]:
        """IN_POSITION 상태인 종목 목록."""
        return [
            t for t, ctx in self._contexts.items()
            if ctx.state == OrderState.IN_POSITION
        ]

    def all_states(self) -> dict[str, str]:
        """전체 종목별 상태 요약."""
        return {t: ctx.state.value for t, ctx in self._contexts.items()}

    def reset(self, ticker: str | None = None) -> None:
        """
        상태 초기화.
        ticker=None이면 전체 초기화.
        """
        if ticker:
            self._contexts.pop(ticker, None)
        else:
            self._contexts.clear()
        logger.info(f"[OrderSM] 상태 초기화: {ticker or '전체'}")
