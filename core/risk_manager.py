import logging
from exchange.upbit_client import UpbitClient, DataFetchError
from data.state_manager import StateManager, Position
import config

logger = logging.getLogger(__name__)


class RiskManager:
    """
    리스크 관리 모듈.

    역할:
    - 포지션별 손절 여부 판단
    - 포트폴리오 최대 낙폭(max drawdown) 초과 여부 판단
    - 신규 매수 가능 여부 최종 확인 (게이트키퍼)
    - Peak equity 워터마크 갱신
    """

    def __init__(
        self,
        client: UpbitClient,
        state: StateManager,
        price_cache=None,
    ) -> None:
        self._client = client
        self._state = state
        self._price_cache = price_cache  # WebSocket PriceCache (optional)

    # ─── 손절 판단 ────────────────────────────────────────────────────────────

    def check_stop_loss(self, position: Position, current_price: float) -> bool:
        """
        손절 기준가 도달 여부.
        LONG: 현재가 <= 손절가
        SHORT: 현재가 >= 손절가
        """
        if position.side == "SHORT":
            triggered = current_price >= position.stop_loss_price
        else:
            triggered = current_price <= position.stop_loss_price

        if triggered:
            loss_pct = abs(position.unrealized_pnl_pct(current_price)) * 100
            logger.warning(
                f"손절 트리거 | {position.ticker} | side={position.side} | "
                f"매수가={position.buy_price:,.0f} "
                f"현재가={current_price:,.0f} "
                f"손실={loss_pct:.2f}%"
            )
        return triggered

    # ─── 최대 낙폭 판단 ───────────────────────────────────────────────────────

    def is_max_drawdown_breached(self) -> bool:
        """
        전체 포트폴리오 평가액 기준 최대 낙폭 초과 여부.
        낙폭 = (peak_equity - current_equity) / peak_equity
        """
        if self._state.peak_equity <= 0:
            return False  # 기준 데이터 없으면 차단 안 함

        current_equity = self.get_total_equity()
        if current_equity <= 0:
            return False

        drawdown = (self._state.peak_equity - current_equity) / self._state.peak_equity

        if drawdown >= config.MAX_DRAWDOWN_PCT:
            logger.warning(
                f"최대 낙폭 초과 | "
                f"peak={self._state.peak_equity:,.0f}원 "
                f"current={current_equity:,.0f}원 "
                f"drawdown={drawdown*100:.2f}%"
            )
            return True

        return False

    def get_total_equity(self) -> float:
        """
        현재 총 평가금액 = KRW 잔고 + 보유 코인 평가금액.
        가격 조회 실패 시 해당 코인 평가금액은 0으로 처리.
        """
        total = 0.0

        try:
            total += self._client.get_balance("KRW")
        except Exception as e:
            logger.warning(f"KRW 잔고 조회 실패: {e}")

        for position in self._state.all_positions():
            price = self._get_price(position.ticker)
            if price:
                if position.side == "SHORT":
                    # SHORT: 투자금 + 미실현 손익
                    total += position.krw_spent + position.unrealized_pnl_krw(price)
                else:
                    total += position.volume * price

        return total

    def _get_price(self, ticker: str) -> float | None:
        """PriceCache 우선, 없으면 REST 조회"""
        if self._price_cache:
            price = self._price_cache.get(ticker)
            if price and not self._price_cache.is_stale(ticker):
                return price
        try:
            return self._client.get_current_price(ticker)
        except Exception:
            return None

    def update_peak_equity(self, current_equity: float) -> None:
        """현재 평가액이 최고점보다 높으면 워터마크 갱신"""
        self._state.update_peak_equity(current_equity)

    # ─── 신규 매수 가능 여부 종합 판단 ───────────────────────────────────────

    def can_open_new_position(self, ticker: str) -> tuple[bool, str]:
        """
        신규 매수 전 최종 게이트 체크.
        반환: (allowed: bool, reason: str)
        """
        # 1. 이미 포지션 보유 중
        if self._state.has_position(ticker):
            return False, f"이미 포지션 보유: {ticker}"

        # 2. KRW 잔고 부족
        try:
            krw = self._client.get_balance("KRW")
            if krw < config.BUDGET_PER_TRADE:
                return False, f"KRW 잔고 부족: {krw:,.0f}원 (필요: {config.BUDGET_PER_TRADE:,}원)"
        except Exception as e:
            return False, f"잔고 조회 실패: {e}"

        # 3. 최대 낙폭 초과
        if self.is_max_drawdown_breached():
            return False, "최대 낙폭 한도 초과 - 신규 매수 차단"

        return True, "OK"
