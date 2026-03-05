"""
가상(Paper) 거래 계좌.
실제 API 호출 없이 시뮬레이션으로 매수/매도 실행.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

import config


@dataclass
class PaperPosition:
    ticker: str
    volume: float
    buy_price: float
    buy_time: datetime
    stop_loss_price: float


@dataclass
class PaperTrade:
    trade_no:    int
    timestamp:   datetime
    account_id:  str
    scenario_id: str
    ticker:      str
    action:      str        # 'BUY' | 'SELL'
    price:       float
    volume:      float
    amount_krw:  float      # 매수: 지출 / 매도: 수령
    fee:         float
    reason:      str
    pnl:         float = 0.0
    pnl_pct:     float = 0.0
    balance_after: float = 0.0
    indicators:  dict = field(default_factory=dict)


class PaperAccount:
    """
    단일 가상 계좌.
    여러 인스턴스를 만들어 여러 전략을 동시에 백테스트 가능.
    """

    def __init__(self, account_id: str, scenario_id: str, initial_balance: float) -> None:
        self.account_id      = account_id
        self.scenario_id     = scenario_id
        self.initial_balance = initial_balance
        self.balance         = float(initial_balance)   # 현금 잔고 (KRW)
        self.positions:      dict[str, PaperPosition] = {}
        self.trade_history:  list[PaperTrade] = []
        self._trade_no       = 0
        self._peak_equity    = float(initial_balance)

    # ─── 포지션 조회 ─────────────────────────────────────────────────────────

    def has_position(self, ticker: str) -> bool:
        return ticker in self.positions

    def get_position(self, ticker: str) -> Optional[PaperPosition]:
        return self.positions.get(ticker)

    def all_positions(self) -> list[PaperPosition]:
        return list(self.positions.values())

    # ─── 매수 ────────────────────────────────────────────────────────────────

    def execute_buy(
        self,
        ticker: str,
        price: float,
        reason: str,
        budget: float | None = None,
        indicators: dict | None = None,
    ) -> Optional[PaperTrade]:
        """시장가 매수 시뮬레이션. 잔고 부족 시 None 반환."""
        amount = min(self.balance, budget or config.BUDGET_PER_TRADE)
        if amount < config.MIN_ORDER_KRW:
            return None

        fee            = amount * config.FEE_RATE
        actual_amount  = amount - fee
        volume         = actual_amount / price
        sl_pct = (indicators or {}).get("stop_loss_pct", config.STOP_LOSS_PCT)
        stop_loss_price = price * (1 - sl_pct)

        self.balance -= amount
        self.positions[ticker] = PaperPosition(
            ticker=ticker,
            volume=volume,
            buy_price=price,
            buy_time=datetime.now(),
            stop_loss_price=stop_loss_price,
        )

        self._trade_no += 1
        trade = PaperTrade(
            trade_no=self._trade_no,
            timestamp=datetime.now(),
            account_id=self.account_id,
            scenario_id=self.scenario_id,
            ticker=ticker,
            action="BUY",
            price=price,
            volume=volume,
            amount_krw=amount,
            fee=fee,
            reason=reason,
            balance_after=self.balance,
            indicators=indicators or {},
        )
        self.trade_history.append(trade)
        return trade

    # ─── 매도 ────────────────────────────────────────────────────────────────

    def execute_sell(
        self,
        ticker: str,
        price: float,
        reason: str,
        indicators: dict | None = None,
    ) -> Optional[PaperTrade]:
        """시장가 매도 시뮬레이션. 포지션 없으면 None 반환."""
        pos = self.positions.pop(ticker, None)
        if pos is None:
            return None

        proceeds    = pos.volume * price
        fee         = proceeds * config.FEE_RATE
        net_proceeds = proceeds - fee
        buy_cost    = pos.volume * pos.buy_price
        pnl         = net_proceeds - buy_cost
        pnl_pct     = pnl / buy_cost * 100 if buy_cost > 0 else 0.0

        self.balance += net_proceeds

        equity = self.get_equity({ticker: price})
        if equity > self._peak_equity:
            self._peak_equity = equity

        self._trade_no += 1
        trade = PaperTrade(
            trade_no=self._trade_no,
            timestamp=datetime.now(),
            account_id=self.account_id,
            scenario_id=self.scenario_id,
            ticker=ticker,
            action="SELL",
            price=price,
            volume=pos.volume,
            amount_krw=net_proceeds,
            fee=fee,
            reason=reason,
            pnl=pnl,
            pnl_pct=pnl_pct,
            balance_after=self.balance,
            indicators=indicators or {},
        )
        self.trade_history.append(trade)
        return trade

    # ─── 손절 체크 ───────────────────────────────────────────────────────────

    def check_stop_loss(self, ticker: str, price: float) -> bool:
        pos = self.positions.get(ticker)
        return pos is not None and price <= pos.stop_loss_price

    # ─── 자산 평가 ───────────────────────────────────────────────────────────

    def get_equity(self, current_prices: dict[str, float] | None = None) -> float:
        cp = current_prices or {}
        equity = self.balance
        for ticker, pos in self.positions.items():
            equity += pos.volume * cp.get(ticker, pos.buy_price)
        return equity

    # ─── 요약 ────────────────────────────────────────────────────────────────

    def get_summary(self, current_prices: dict[str, float] | None = None) -> dict:
        equity     = self.get_equity(current_prices)
        total_pnl  = equity - self.initial_balance
        pnl_pct    = total_pnl / self.initial_balance * 100 if self.initial_balance else 0.0
        sell_trades = [t for t in self.trade_history if t.action == "SELL"]
        wins        = [t for t in sell_trades if t.pnl > 0]

        return {
            "account_id":      self.account_id,
            "scenario_id":     self.scenario_id,
            "initial_balance": self.initial_balance,
            "current_equity":  equity,
            "cash_balance":    self.balance,
            "total_pnl":       total_pnl,
            "total_pnl_pct":   pnl_pct,
            "peak_equity":     self._peak_equity,
            "total_trades":    len(self.trade_history),
            "buy_count":       len([t for t in self.trade_history if t.action == "BUY"]),
            "sell_count":      len(sell_trades),
            "win_rate":        len(wins) / len(sell_trades) * 100 if sell_trades else 0.0,
            "open_positions":  len(self.positions),
        }
