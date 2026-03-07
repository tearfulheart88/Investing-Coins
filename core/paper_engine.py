"""
가상거래 엔진.
여러 PaperAccount + 전략 조합을 동시에 실행.
실제 API 주문 없이 WebSocket/REST 가격만 사용.
"""
from __future__ import annotations
import time
import logging
import threading
from datetime import date, datetime
from zoneinfo import ZoneInfo

import config
from core.paper_account import PaperAccount, PaperTrade
from data.market_data import MarketData
from exchange.websocket_manager import PriceCache
from strategies.registry import load_strategy

logger = logging.getLogger(__name__)
KST = ZoneInfo("Asia/Seoul")


class PaperScenario:
    """계좌 + 전략 쌍. PaperEngine 내부에서 사용."""

    def __init__(
        self,
        account: PaperAccount,
        market_data: MarketData,
        strategy_id: str,
        scenario_id: str,
        tickers: list[str] | None = None,
        budget_per_trade: int | None = None,
        budget_pct: float | None = None,
    ) -> None:
        self.account  = account
        self.strategy = load_strategy(market_data, strategy_id, scenario_id)
        self.tickers  = tickers or list(config.TICKERS)
        self.budget_per_trade = budget_per_trade or config.BUDGET_PER_TRADE
        self.budget_pct = budget_pct                 # None이면 고정액, 값 있으면 잔고의 N%
        self._daily_buy_tracker: dict[str, date] = {}

    def already_bought_today(self, ticker: str) -> bool:
        return self._daily_buy_tracker.get(ticker) == datetime.now(KST).date()

    def mark_bought(self, ticker: str) -> None:
        self._daily_buy_tracker[ticker] = datetime.now(KST).date()

    def reset_daily(self) -> None:
        self._daily_buy_tracker.clear()


class PaperEngine:
    """
    여러 PaperScenario를 단일 루프에서 동시 실행.
    WebSocketManager/MarketData는 외부에서 주입 (실거래와 공유 가능).
    """

    def __init__(
        self,
        scenarios: list[PaperScenario],
        market_data: MarketData,
        price_cache: PriceCache,
        tickers: list[str],
        obsidian_logger=None,
    ) -> None:
        self._scenarios     = scenarios
        self._market_data   = market_data
        self._price_cache   = price_cache
        self._tickers       = tickers
        self._obsidian      = obsidian_logger
        self._stop_event    = threading.Event()
        self._thread: threading.Thread | None = None
        self.running        = False

    # ─── 시작 / 종료 ─────────────────────────────────────────────────────────

    def start(self) -> None:
        self._stop_event.clear()
        self.running = True
        self._thread = threading.Thread(
            target=self._run_loop, name="PaperEngine", daemon=True
        )
        self._thread.start()
        tickers_info = ", ".join(
            f"{s.account.account_id}({len(s.tickers)}종목)" for s in self._scenarios
        )
        logger.info(f"가상거래 엔진 시작 | 시나리오 {len(self._scenarios)}개 | {tickers_info}")

        # 옵시디언 세션 시작 기록
        if self._obsidian:
            try:
                scenario_info = [
                    {
                        "scenario_id": s.account.scenario_id,
                        "is_paper": True,
                        "initial_balance": s.account.balance,
                    }
                    for s in self._scenarios
                ]
                self._obsidian.log_session_start(scenario_info)
            except Exception as e:
                logger.warning(f"Obsidian 세션 시작 기록 실패: {e}")

    def stop(self) -> None:
        # ── 종료 전 전 포지션 강제 청산 ──
        prices = self._price_cache.all_prices()
        for scenario in self._scenarios:
            account = scenario.account
            for ticker in list(account.positions.keys()):
                price = prices.get(ticker) or account.positions[ticker].buy_price
                try:
                    indicators = self._get_indicators(ticker, price)
                except Exception:
                    indicators = {}
                trade = account.execute_sell(ticker, price, "STOP_LIQUIDATION", indicators)
                if trade:
                    scenario.strategy.on_position_closed(ticker)
                    logger.info(
                        f"[{account.account_id}] 정지 청산 | "
                        f"{ticker} | {price:,.0f}원 | PnL={trade.pnl:+,.0f}원"
                    )
                    self._log_obsidian(account.scenario_id, trade)

        if self._obsidian:
            # ── 세션 종료 요약 ──
            try:
                summaries = self.get_all_summaries()
                for s in summaries:
                    s["is_paper"] = True
                self._obsidian.log_session_end(summaries)
            except Exception as e:
                logger.warning(f"Obsidian 세션 종료 기록 실패: {e}")

            # ── 시나리오별 일보 저장 ──
            for scenario in self._scenarios:
                try:
                    summary = scenario.account.get_summary(prices)
                    summary["is_paper"] = True
                    self._obsidian.log_daily_report(
                        scenario.account.scenario_id,
                        summary,
                        scenario.account.trade_history,
                        is_paper=True,
                    )
                except Exception as e:
                    logger.warning(
                        f"Obsidian 일보 실패 [{scenario.account.account_id}]: {e}"
                    )

        self._stop_event.set()
        self.running = False
        if self._thread:
            self._thread.join(timeout=5.0)
        logger.info("가상거래 엔진 종료")

    # ─── 요약 ────────────────────────────────────────────────────────────────

    def get_all_summaries(self) -> list[dict]:
        prices = self._price_cache.all_prices()
        return [s.account.get_summary(prices) for s in self._scenarios]

    def get_scenario(self, account_id: str) -> PaperScenario | None:
        return next((s for s in self._scenarios if s.account.account_id == account_id), None)

    # ─── 메인 루프 ───────────────────────────────────────────────────────────

    def _run_loop(self) -> None:
        logger.info("가상거래 루프 시작")
        _last_daily_sell_date: date | None = None   # 오늘 스케줄 매도 완료 추적

        while not self._stop_event.is_set():
            loop_start = time.time()
            now_kst    = datetime.now(KST)
            today      = now_kst.date()

            # ── 09:00 KST 스케줄 매도 (VB 전략 대상, 하루 1회) ──────────────
            if (
                now_kst.hour   == config.SELL_HOUR_KST
                and now_kst.minute == config.SELL_MINUTE_KST
                and today != _last_daily_sell_date
            ):
                _last_daily_sell_date = today
                self._execute_daily_sell()

            for scenario in self._scenarios:
                if self._stop_event.is_set():
                    break
                for ticker in scenario.tickers:
                    try:
                        self._process(scenario, ticker)
                    except Exception as e:
                        logger.warning(f"[{scenario.account.account_id}][{ticker}] 처리 오류: {e}", exc_info=True)

            elapsed = time.time() - loop_start
            self._stop_event.wait(timeout=max(0.0, config.PRICE_CHECK_INTERVAL_SEC - elapsed))

    def _execute_daily_sell(self) -> None:
        """
        09:00 KST 스케줄 매도.
        requires_scheduled_sell()=True 전략(VB 계열)의 모든 포지션을 청산하고
        시나리오별 일별 리포트를 Obsidian에 저장.
        """
        logger.info("[PaperEngine] 09:00 스케줄 매도 시작")
        prices = self._price_cache.all_prices()

        for scenario in self._scenarios:
            if not scenario.strategy.requires_scheduled_sell():
                continue

            account = scenario.account

            # 보유 포지션 전체 매도
            for ticker in list(account.positions.keys()):
                price = prices.get(ticker) or account.positions[ticker].buy_price
                try:
                    indicators = self._get_indicators(ticker, price)
                except Exception:
                    indicators = {}
                trade = account.execute_sell(ticker, price, "SCHEDULED_09H", indicators)
                if trade:
                    logger.info(
                        f"[{account.account_id}] 스케줄 매도 | "
                        f"{ticker} | {price:,.0f}원 | PnL={trade.pnl:+,.0f}원"
                    )
                    self._log_obsidian(account.scenario_id, trade)

            scenario.reset_daily()
            scenario.strategy.reset_daily()   # 전략 내부 상태 초기화 (peak, 타임컷 등)

            # ── 일보 저장 ──
            if self._obsidian:
                try:
                    summary = account.get_summary(prices)
                    summary["is_paper"] = True
                    self._obsidian.log_daily_report(
                        account.scenario_id,
                        summary,
                        account.trade_history,
                        is_paper=True,
                    )
                    logger.info(
                        f"[PaperEngine] 일보 저장 완료 | {account.account_id}"
                    )
                except Exception as e:
                    logger.warning(
                        f"Obsidian 일보 실패 [{account.account_id}]: {e}"
                    )

        logger.info("[PaperEngine] 09:00 스케줄 매도 완료")

    def _process(self, scenario: PaperScenario, ticker: str) -> None:
        account  = scenario.account
        strategy = scenario.strategy

        price = self._price_cache.get(ticker)
        if not price:
            return

        # ── 포지션 있을 때 ──
        if account.has_position(ticker):
            pos = account.get_position(ticker)

            # 손절 체크
            if account.check_stop_loss(ticker, price):
                indicators = self._get_indicators(ticker, price)
                trade = account.execute_sell(ticker, price, "손절매", indicators)
                if trade:
                    strategy.on_position_closed(ticker)
                    logger.info(
                        f"[{account.account_id}] 손절 | {ticker} | {price:,.0f}원 | "
                        f"PnL: {trade.pnl:+,.0f}원 ({trade.pnl_pct:+.2f}%)"
                    )
                    self._log_obsidian(scenario.account.scenario_id, trade)
                return

            # 전략 매도 신호
            from data.state_manager import Position as RealPos
            strat_pos = RealPos(
                ticker=ticker,
                volume=pos.volume,
                buy_price=pos.buy_price,
                buy_time=pos.buy_time,
                krw_spent=pos.volume * pos.buy_price,
                order_uuid="PAPER",
                stop_loss_price=pos.stop_loss_price,
                strategy_id=strategy.get_strategy_id(),
                scenario_id=strategy.get_scenario_id(),
            )
            sell_signal = strategy.should_sell_on_signal(ticker, price, strat_pos)
            if sell_signal.should_sell:
                indicators = self._get_indicators(ticker, price)
                trade = account.execute_sell(ticker, price, sell_signal.reason, indicators)
                if trade:
                    strategy.on_position_closed(ticker)
                    logger.info(
                        f"[{account.account_id}] 매도신호 | {ticker} | "
                        f"PnL: {trade.pnl:+,.0f}원 ({trade.pnl_pct:+.2f}%)"
                    )
                    self._log_obsidian(scenario.account.scenario_id, trade)

        # ── 포지션 없을 때 ──
        else:
            # 스케줄 매도 전략(VB 계열)만 하루 1회 제한 적용
            if strategy.requires_scheduled_sell() and scenario.already_bought_today(ticker):
                return

            buy_signal = strategy.should_buy(ticker, price)
            if buy_signal.should_buy:
                indicators = self._get_indicators(ticker, price)
                # 전략별 stop_loss_pct 등 메타데이터 병합 (스캘핑 전략의 0.3% SL 등)
                if buy_signal.metadata:
                    indicators.update(buy_signal.metadata)
                # 예산 계산: budget_pct 있으면 잔고의 N%, 없으면 고정액
                if scenario.budget_pct is not None:
                    buy_budget = int(account.balance * scenario.budget_pct / 100)
                    buy_budget = max(buy_budget, config.MIN_ORDER_KRW)
                else:
                    buy_budget = scenario.budget_per_trade

                trade = account.execute_buy(
                    ticker, price, buy_signal.reason,
                    budget=buy_budget,
                    indicators=indicators,
                )
                if trade:
                    scenario.mark_bought(ticker)
                    logger.info(
                        f"[{account.account_id}] 매수 | {ticker} | {price:,.0f}원 | "
                        f"잔고: {account.balance:,.0f}원"
                    )
                    self._log_obsidian(scenario.account.scenario_id, trade)

    # ─── 지표 수집 ───────────────────────────────────────────────────────────

    def _get_indicators(self, ticker: str, price: float) -> dict:
        ind = {"현재가": price}
        try:
            df = self._market_data.get_ohlcv(ticker, count=20)
            if df is not None and not df.empty:
                ind["시가"]   = float(df["open"].iloc[-1])
                ind["고가"]   = float(df["high"].iloc[-1])
                ind["저가"]   = float(df["low"].iloc[-1])
                ind["거래량"] = float(df["volume"].iloc[-1])

            k = self._market_data.compute_noise_filter_k(ticker, config.NOISE_FILTER_DAYS)
            ind["노이즈_k"] = round(k, 4)

            target = self._market_data.compute_target_price(ticker, k)
            ind["목표가"] = round(target, 0)

            ma = self._market_data.compute_ma(ticker, config.MA_PERIOD)
            ind[f"MA{config.MA_PERIOD}"] = round(ma, 0)

            rsi = self._market_data.compute_rsi(ticker, config.RSI_PERIOD)
            ind[f"RSI{config.RSI_PERIOD}"] = round(rsi, 2)
        except Exception:
            pass
        return ind

    # ─── 옵시디언 로그 ───────────────────────────────────────────────────────

    def _log_obsidian(self, scenario_id: str, trade: PaperTrade) -> None:
        if self._obsidian:
            try:
                self._obsidian.log_trade(scenario_id, trade, is_paper=True)
            except Exception as e:
                logger.warning(f"Obsidian 로그 실패: {e}")
