from abc import ABC, abstractmethod
from dataclasses import dataclass, field


@dataclass
class BuySignal:
    ticker: str
    should_buy: bool
    current_price: float
    reason: str                           # "BREAKOUT+MA" / "RSI_OVERSOLD" / "BB_LOWER" 등
    metadata: dict = field(default_factory=dict)
    # metadata: 전략별 부가 정보 (k, target_price, rsi, bb_lower 등)


@dataclass
class SellSignal:
    ticker: str
    should_sell: bool
    current_price: float
    reason: str                           # "STRATEGY_EXIT" / "" (해당 없음)


class BaseStrategy(ABC):
    """
    모든 전략 파일이 반드시 구현해야 할 인터페이스.

    규칙:
    - 각 전략 파일은 이 파일과 data/market_data.py만 임포트
    - 전략 파일끼리 서로 임포트 금지 (완전 독립)
    - 새 전략 = 이 클래스 상속 + strategies/registry.py 한 줄 추가
    """

    @abstractmethod
    def get_strategy_id(self) -> str:
        """예: 'volatility_breakout'"""

    @abstractmethod
    def get_scenario_id(self) -> str:
        """예: 'vb_noise_filter'"""

    @abstractmethod
    def requires_scheduled_sell(self) -> bool:
        """
        True  → 익일 09:00 KST 스케줄 매도 사용 (VB 계열)
        False → 전략 자체 신호로 청산 (RSI, Bollinger, Grid)
        """

    @abstractmethod
    def should_buy(self, ticker: str, current_price: float) -> BuySignal:
        """
        매수 신호 판단.
        데이터 조회 실패 시 should_buy=False, reason="DATA_ERROR" 반환 (안전 실패).
        """

    @abstractmethod
    def should_sell_on_signal(
        self, ticker: str, current_price: float, position
    ) -> SellSignal:
        """
        전략 고유 매도 신호 판단.
        VB 계열: 항상 SellSignal(should_sell=False) 반환
        RSI: RSI >= 70 시 True
        Bollinger: 중심선 상향 돌파 시 True
        """
