"""
거래소 클라이언트 추상 인터페이스.

모든 거래소(Upbit, Binance 등)는 이 인터페이스를 구현.
OrderResult도 여기서 정의 (거래소 공통).
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass
class OrderResult:
    """체결 확인 완료된 주문 결과"""
    uuid: str
    ticker: str
    side: str               # 'bid' (매수) / 'ask' (매도)
    volume: float           # 체결 수량
    avg_price: float        # 평균 체결가
    paid_fee: float         # 수수료
    state: str              # 'done' / 'cancel' 등


class BaseExchangeClient(ABC):
    """
    거래소 클라이언트 추상 베이스.
    현물/선물 공통 인터페이스.
    """

    @abstractmethod
    def get_balance(self, ticker: str) -> float:
        """잔고 조회. ticker='KRW' → 원화, 'KRW-BTC' → BTC 수량"""

    @abstractmethod
    def get_balances(self) -> list:
        """전체 보유 자산 목록 조회"""

    @abstractmethod
    def buy_market_order(self, ticker: str, amount) -> OrderResult:
        """시장가 매수"""

    @abstractmethod
    def sell_market_order(self, ticker: str, volume: float) -> OrderResult:
        """시장가 매도"""

    @abstractmethod
    def get_current_price(self, ticker: str) -> float:
        """현재가 조회"""
