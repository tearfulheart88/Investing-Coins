"""
전략: 등차 간격 그리드 트레이딩 (미구현 - 추후 확장)
시나리오 ID: grid_arithmetic

설명:
  설정된 가격 범위 내에서 동일 간격으로 매수/매도 주문을 격자 배치.
  일반적인 단일 포지션 루프와 다른 별도 실행 로직 필요.
  현재 NotImplementedError 발생 → 추후 별도 모듈 구현 예정.

임포트 규칙:
  이 파일은 base_strategy, data.market_data 만 임포트.
"""
from data.market_data import MarketData
from strategies.base_strategy import BaseStrategy, BuySignal, SellSignal


class GridArithmeticStrategy(BaseStrategy):

    def __init__(self, market_data: MarketData) -> None:
        self._md = market_data

    def get_strategy_id(self) -> str:
        return "grid_trading"

    def get_scenario_id(self) -> str:
        return "grid_arithmetic"

    def requires_scheduled_sell(self) -> bool:
        return False

    def should_buy(self, ticker: str, current_price: float) -> BuySignal:
        raise NotImplementedError(
            "Grid 전략은 단순 매수 신호 루프를 사용하지 않습니다. "
            "별도 그리드 실행 모듈 구현 필요."
        )

    def should_sell_on_signal(self, ticker, current_price, position) -> SellSignal:
        raise NotImplementedError(
            "Grid 전략은 단순 매도 신호 루프를 사용하지 않습니다."
        )
