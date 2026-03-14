from abc import ABC, abstractmethod
from dataclasses import dataclass, field


# ─── 통합 Signal (LONG/SHORT 양방향 지원) ────────────────────────────────────

@dataclass
class Signal:
    """
    통합 매매 신호. LONG/SHORT 양방향 지원.

    action: "ENTER" (진입) | "EXIT" (청산) | "NONE" (관망)
    side:   "LONG" | "SHORT"
    should_act: True면 action 실행
    """
    ticker: str
    action: str = "NONE"
    side: str = "LONG"
    should_act: bool = False
    current_price: float = 0.0
    reason: str = ""
    metadata: dict = field(default_factory=dict)

    @staticmethod
    def enter(ticker: str, price: float, reason: str,
              side: str = "LONG", **meta) -> "Signal":
        return Signal(
            ticker=ticker, action="ENTER", side=side,
            should_act=True, current_price=price,
            reason=reason, metadata=meta,
        )

    @staticmethod
    def exit(ticker: str, price: float, reason: str,
             side: str = "LONG", **meta) -> "Signal":
        return Signal(
            ticker=ticker, action="EXIT", side=side,
            should_act=True, current_price=price,
            reason=reason, metadata=meta,
        )

    @staticmethod
    def no_action(ticker: str, price: float, reason: str = "") -> "Signal":
        return Signal(
            ticker=ticker, action="NONE", side="LONG",
            should_act=False, current_price=price, reason=reason,
        )

    @staticmethod
    def from_buy(bs: "BuySignal") -> "Signal":
        """BuySignal → Signal 변환 (하위호환)"""
        return Signal(
            ticker=bs.ticker,
            action="ENTER" if bs.should_buy else "NONE",
            side="LONG",
            should_act=bs.should_buy,
            current_price=bs.current_price,
            reason=bs.reason,
            metadata=bs.metadata,
        )

    @staticmethod
    def from_sell(ss: "SellSignal") -> "Signal":
        """SellSignal → Signal 변환 (하위호환)"""
        return Signal(
            ticker=ss.ticker,
            action="EXIT" if ss.should_sell else "NONE",
            side="LONG",
            should_act=ss.should_sell,
            current_price=ss.current_price,
            reason=ss.reason,
            metadata=ss.metadata,
        )


# ─── 기존 BuySignal / SellSignal (하위호환 유지) ─────────────────────────────

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
    metadata: dict = field(default_factory=dict)


class BaseStrategy(ABC):
    """
    모든 전략 파일이 반드시 구현해야 할 인터페이스.

    규칙:
    - 각 전략 파일은 이 파일과 data/market_data.py만 임포트
    - 전략 파일끼리 서로 임포트 금지 (완전 독립)
    - 새 전략 = 이 클래스 상속 + strategies/registry.py 한 줄 추가
    """

    # Trader.__init__() 에서 OrderbookCache 주입 (선택적)
    _orderbook_cache = None  # type: ignore

    def inject_orderbook(self, cache) -> None:  # type: ignore
        """호가창 캐시 주입. Trader가 초기화 후 호출."""
        self._orderbook_cache = cache

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

    # ─── 선택적 라이프사이클 콜백 (서브클래스에서 오버라이드 가능) ──────────

    def on_position_closed(self, ticker: str, reason: str = "") -> None:
        """
        포지션 종료 시 호출 (매도, 손절, 스케줄 매도 등 모든 경로).
        내부 추적 상태(peak, 타임컷 연장 등)를 정리한다.
    reason: 매도 사유 ("ENGINE_STOP", "HARD_SL", "TRAIL_DROP", "SCHEDULED_09H" 등)
        기본 구현: 아무것도 하지 않음.
        """

    def on_position_reentered(
        self,
        ticker: str,
        new_entry_price: float,
        reason: str = "",
    ) -> None:
        """
        ?ъ쭊??湲곗??媛 媛깆떊 ???몄텧.
        ?꾨왂 ?대? peak/??꾩뻔 湲곗? ?곹깭瑜??좉퇋 吏꾩엯媛濡?珥덇린?뷀븷 ???ъ슜?쒕떎.
        湲곕낯 援ы쁽: ?꾨Т寃껊룄 ?섏? ?딆쓬.
        """

    def reset_daily(self) -> None:
        """
        09:00 스케줄 매도 완료 후 호출. 일별 내부 상태 초기화.
        기본 구현: 아무것도 하지 않음.
        """

    def get_history_requirements(self) -> dict[str, int]:
        """
        전략 진입 평가 전에 필요한 최소 봉 수.

        반환 예시:
          {
              "day": 15,
              "minute60": 200,
              "minute5": 60,
          }

        신규 상장/재상장 종목의 반복 DATA_ERROR 로그를 줄이기 위한
        사전 가드로 사용된다.
        """
        return {}

    def get_ticker_selection_profile(self) -> dict:
        """
        전략별 종목 선별 힌트.

        예시:
          {
              "pattern": "mean_reversion_rsi",
              "pool_size": 80,
          }

        반환값은 data.market_data 의 전략별 종목 선별기에서 사용된다.
        기본 구현은 빈 dict 이며, 이 경우 시나리오 ID 기반 기본 프로필을 사용한다.
        """
        return {}
