"""
ATR 기반 포지션 사이징 — PositionSizer

■ 공식:
  risk_krw  = equity × risk_per_trade
  stop_dist = |entry_price − sl_price|
  qty       = risk_krw / stop_dist
  order_krw = qty × entry_price

■ 제약:
  - order_krw >= min_order_krw (업비트 최소 5,000원)
  - order_krw <= max_order_krw (최대 투자금 제한)
  - qty > 0

■ 사용법:
  sizer = PositionSizer(risk_per_trade=0.005, min_order_krw=5000)
  result = sizer.calculate(
      equity_krw=1_000_000,
      entry_price=50_000_000,
      atr=800_000,
      sl_atr_mult=1.5,
  )
  if result.valid:
      buy(ticker, result.order_krw)
"""

import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class SizingResult:
    """포지션 사이징 결과."""
    valid: bool = False           # 진입 가능 여부
    order_krw: float = 0.0        # 투자 금액 (KRW)
    qty: float = 0.0              # 수량
    sl_price: float = 0.0         # 계산된 손절가
    sl_dist: float = 0.0          # 진입가 - 손절가
    risk_krw: float = 0.0         # 1회 리스크 금액
    risk_pct: float = 0.0         # 1회 리스크 비율
    reason: str = ""              # 무효 사유


class PositionSizer:
    """
    ATR 기반 포지션 사이징 계산기.

    Parameters
    ----------
    risk_per_trade : float
        계좌 대비 1회 리스크 비율. 기본 0.5% (0.005).
    min_order_krw : float
        업비트 최소 주문 금액. 기본 5,000원.
    max_order_krw : float
        1종목당 최대 투자 금액 제한. 기본 500,000원.
    max_position_pct : float
        계좌 대비 1종목 최대 비중. 기본 20% (0.20).
    """

    def __init__(
        self,
        risk_per_trade: float = 0.005,
        min_order_krw: float = 5_000,
        max_order_krw: float = 500_000,
        max_position_pct: float = 0.20,
    ) -> None:
        self._risk_per_trade = risk_per_trade
        self._min_order_krw = min_order_krw
        self._max_order_krw = max_order_krw
        self._max_position_pct = max_position_pct

    def calculate(
        self,
        equity_krw: float,
        entry_price: float,
        atr: float,
        sl_atr_mult: float = 1.5,
        risk_per_trade: float | None = None,
    ) -> SizingResult:
        """
        포지션 사이즈 계산.

        Parameters
        ----------
        equity_krw : 계좌 총 자산 (KRW)
        entry_price : 진입 예상 가격
        atr : ATR (가격 단위)
        sl_atr_mult : 손절 ATR 배수 (SL = entry - atr × mult)
        risk_per_trade : 리스크 비율 오버라이드 (None이면 기본값)

        Returns
        -------
        SizingResult
        """
        r = risk_per_trade or self._risk_per_trade

        # 검증
        if equity_krw <= 0:
            return SizingResult(reason="EQUITY_ZERO")
        if entry_price <= 0:
            return SizingResult(reason="ENTRY_ZERO")
        if atr <= 0:
            return SizingResult(reason="ATR_ZERO")

        # 손절가 계산
        sl_dist = atr * sl_atr_mult
        sl_price = entry_price - sl_dist

        if sl_price <= 0:
            return SizingResult(reason=f"SL_NEGATIVE({sl_price:.0f})")
        if sl_dist <= 0:
            return SizingResult(reason="SL_DIST_ZERO")

        # 리스크 금액
        risk_krw = equity_krw * r

        # 수량 & 투자금
        qty = risk_krw / sl_dist
        order_krw = qty * entry_price

        # 제약 1: 최소 주문 금액
        if order_krw < self._min_order_krw:
            return SizingResult(
                reason=f"BELOW_MIN_ORDER({order_krw:.0f}<{self._min_order_krw:.0f})"
            )

        # 제약 2: 최대 투자금 제한
        max_by_config = self._max_order_krw
        max_by_equity = equity_krw * self._max_position_pct
        cap = min(max_by_config, max_by_equity)

        if order_krw > cap:
            # 캡 적용: 투자금을 줄이고 수량도 재계산
            order_krw = cap
            qty = order_krw / entry_price
            # 리스크도 재계산 (고정 사이즈로 전환)
            risk_krw = qty * sl_dist
            r = risk_krw / equity_krw

        return SizingResult(
            valid=True,
            order_krw=round(order_krw, 0),
            qty=qty,
            sl_price=sl_price,
            sl_dist=sl_dist,
            risk_krw=round(risk_krw, 0),
            risk_pct=r,
        )

    def calculate_from_sl_price(
        self,
        equity_krw: float,
        entry_price: float,
        sl_price: float,
        risk_per_trade: float | None = None,
    ) -> SizingResult:
        """
        손절가가 이미 정해진 경우의 사이징.
        (전략에서 직접 SL을 계산한 경우 사용)
        """
        if entry_price <= 0 or sl_price <= 0:
            return SizingResult(reason="PRICE_ZERO")

        sl_dist = abs(entry_price - sl_price)
        if sl_dist <= 0:
            return SizingResult(reason="SL_DIST_ZERO")

        # ATR 역산 (sl_atr_mult=1.0으로 치환)
        return self.calculate(
            equity_krw=equity_krw,
            entry_price=entry_price,
            atr=sl_dist,
            sl_atr_mult=1.0,
            risk_per_trade=risk_per_trade,
        )
