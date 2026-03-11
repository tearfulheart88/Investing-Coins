import time
import threading
import logging
from dataclasses import dataclass, field
from datetime import datetime, date
from zoneinfo import ZoneInfo

import config
from exchange.upbit_client import UpbitClient, OrderResult, OrderFailedError, InsufficientBalanceError
from exchange.websocket_manager import WebSocketManager, PriceCache
from exchange.orderbook_manager import OrderbookManager, OrderbookCache
from data.market_data import MarketData
from data.state_manager import StateManager, Position
from strategies.registry import load_strategy
from core.risk_manager import RiskManager
from core.scheduler import TradingScheduler
from core.ticker_manager import DynamicTickerManager
from core.auto_tuner import AutoTuner, SymbolMetrics
from core.universe_selector import UniverseSelector
from core.position_sizer import PositionSizer
from core.order_state_machine import OrderStateMachine, OrderState
from logging_.trade_logger import TradeLogger, TradeRecord, now_kst
from logging_.session_manager import SessionManager

logger = logging.getLogger(__name__)

KST = ZoneInfo("Asia/Seoul")


@dataclass
class RealScenario:
    """실거래 멀티전략 시나리오."""
    strategy_id: str
    scenario_id: str
    strategy: object          # BaseStrategy 인스턴스
    tickers: list[str] = field(default_factory=list)
    ticker_count: int = 10             # 거래량 상위 N개 (동적 갱신 기준)
    weight_pct: float = 100.0          # 전체 자금 중 이 시나리오 비중 (%)
    budget_pct: float = 30.0           # 시나리오 자금 중 1회 거래 비중 (%)
    daily_buy_tracker: dict = field(default_factory=dict)


class Trader:
    """
    자동매매 시스템 중앙 조율자.

    v2 개선:
    - 거래 잠금(Lock): 스케줄러/메인루프 동시 매도 경쟁 방지
    - 일일 매수 추적: VB 전략의 하루 1회 매수 보장
    - 매도 실패 시 포지션 유지: 자산 추적 유실 방지
    - OrderResult 타입: 체결 확인된 실체결 수량/가격 보장
    """

    def __init__(self, scenarios: list[dict] | None = None) -> None:
        logger.info("=== Upbit Auto-Trading System 초기화 ===")

        self.client      = UpbitClient(config.ACCESS_KEY, config.SECRET_KEY)
        self.price_cache = PriceCache()
        self.market_data = MarketData()
        self.state       = StateManager(config.POSITIONS_PATH)

        # ── 멀티시나리오 / 단일전략 초기화 ──────────────────────────────────────
        if scenarios:
            self._scenarios: list[RealScenario] = []
            all_tickers: set[str] = set()
            for s in scenarios:
                strat = load_strategy(self.market_data, s["strategy_id"], s["scenario_id"])
                initial_tickers = s.get("tickers", list(config.TICKERS))
                rs = RealScenario(
                    strategy_id=s["strategy_id"],
                    scenario_id=s["scenario_id"],
                    strategy=strat,
                    tickers=initial_tickers,
                    ticker_count=s.get("ticker_count", len(initial_tickers)),
                    weight_pct=s.get("weight_pct", 100.0),
                    budget_pct=s.get("budget_pct", config.BUDGET_PER_TRADE_PCT),
                )
                self._scenarios.append(rs)
                all_tickers.update(rs.tickers)
            self._active_tickers: list[str] = list(all_tickers)
            # 멀티시나리오: 주기적 거래량 상위 갱신용 타임스탬프
            # (time.time()으로 초기화 → 최초 갱신은 TICKER_REFRESH_HOURS 후 발생)
            self._last_ticker_refresh: float = time.time()
            self._multi_scenario_mode: bool = True
        else:
            # 단일전략 호환 모드 (기존 config 기반)
            strat = load_strategy(
                self.market_data, config.SELECTED_STRATEGY, config.SELECTED_SCENARIO,
            )
            self._scenarios = [RealScenario(
                strategy_id=config.SELECTED_STRATEGY,
                scenario_id=config.SELECTED_SCENARIO,
                strategy=strat,
                tickers=list(config.TICKERS),
                ticker_count=config.TOP_TICKERS_COUNT,
                weight_pct=100.0,
                budget_pct=config.BUDGET_PER_TRADE_PCT,
            )]
            self._active_tickers = list(config.TICKERS)
            self._last_ticker_refresh: float = 0.0
            self._multi_scenario_mode: bool = False

        # 첫 시나리오 strategy를 self.strategy로 유지 (scheduler 등 하위호환)
        self.strategy = self._scenarios[0].strategy

        # ── 동적 종목 선택 ────────────────────────────────────────────────────
        # 멀티시나리오(scenarios 제공 시): _refresh_scenario_tickers()로 주기적 갱신
        # 단일시나리오(config 기반): USE_DYNAMIC_TICKERS=True 시 DynamicTickerManager 사용
        self._ticker_manager: DynamicTickerManager | None = None
        if config.USE_DYNAMIC_TICKERS and not scenarios:
            self._ticker_manager = DynamicTickerManager(
                n=config.TOP_TICKERS_COUNT,
                blacklist=config.TICKER_BLACKLIST,
                refresh_hours=config.TICKER_REFRESH_HOURS,
            )
            try:
                self._active_tickers = self._ticker_manager.get_current_tickers()
                logger.info(f"동적 종목 로드 완료: {self._active_tickers}")
            except RuntimeError as e:
                logger.warning(f"동적 종목 로드 실패 → config.TICKERS 폴백: {e}")
                self._active_tickers = list(config.TICKERS)

        # WebSocket 피드: _active_tickers로 구독
        self.ws   = WebSocketManager(self._active_tickers, self.price_cache)
        self.risk = RiskManager(self.client, self.state, self.price_cache)
        self.trade_logger    = TradeLogger()
        first_scenario_id = self._scenarios[0].scenario_id
        self.session_manager = SessionManager(first_scenario_id)
        self.scheduler       = TradingScheduler(self, self.market_data, self.trade_logger)
        self.obsidian_logger = None   # ObsidianLogger 주입 가능 (ui.py에서 설정)

        # ── 호가 WebSocket (스프레드 계산) ────────────────────────────────────
        self.orderbook_cache = OrderbookCache()
        self._ob_manager: OrderbookManager | None = None
        if config.ORDERBOOK_WS_ENABLED:
            self._ob_manager = OrderbookManager(self._active_tickers, self.orderbook_cache)

        # ── AutoTuner (ATR% 기반 파라미터 자동 조정) ──────────────────────────
        self.auto_tuner: AutoTuner | None = None
        if config.USE_AUTO_TUNER:
            self.auto_tuner = AutoTuner(
                fee_edge_mult=config.FEE_EDGE_MULT,
                risk_per_trade=config.RISK_PER_TRADE,
            )

        # ── UniverseSelector (스코어 기반 종목 선정) ──────────────────────────
        self.universe_selector: UniverseSelector | None = None
        if config.USE_SCORE_SELECTION:
            self.universe_selector = UniverseSelector(
                orderbook_cache=self.orderbook_cache if config.ORDERBOOK_WS_ENABLED else None,
                market_data=self.market_data,
                min_24h_value_krw=config.MIN_24H_VALUE_KRW,
                max_spread_bps=config.MAX_SPREAD_BPS,
                additional_blacklist=config.TICKER_BLACKLIST,
            )

        # ── PositionSizer (ATR 기반 포지션 사이징) ────────────────────────────
        self.position_sizer: PositionSizer | None = None
        if config.USE_ATR_SIZING:
            self.position_sizer = PositionSizer(
                risk_per_trade=config.RISK_PER_TRADE,
                min_order_krw=config.MIN_ORDER_KRW,
                max_order_krw=config.MAX_ORDER_KRW,
                max_position_pct=config.MAX_POSITION_PCT,
            )

        # ── OrderStateMachine (주문 생명주기) ─────────────────────────────────
        self.order_sm = OrderStateMachine(
            entry_timeout_sec=config.ORDER_SM_ENTRY_TIMEOUT_SEC,
            exit_timeout_sec=config.ORDER_SM_EXIT_TIMEOUT_SEC,
        )

        # 거래 잠금: 스케줄러(09:00매도)와 메인루프 동시 실행 방지
        self._trade_lock = threading.Lock()

        # Obsidian 일보용 세션 거래 누적
        self._obs_session_trades: list = []   # PaperTrade 객체 리스트
        self._session_start_equity: float = 0.0

        # 에러 카운터 (연속 오류 감지)
        self._error_count = 0
        self._error_window_start = time.time()

        # 매도 실패 쿨다운 (같은 종목 연속 실패 시 재시도 억제)
        # 3회 연속 실패 → 5분간 매도 시도 중단 (log 폭발 방지)
        self._sell_fail_count: dict[str, int] = {}    # ticker → 연속 실패 횟수
        self._sell_cooldown: dict[str, float] = {}    # ticker → 쿨다운 만료 timestamp

        # 런타임 자동 블랙리스트 (세션 중 데이터 오류 반복 종목 자동 제외)
        # 상장폐지/거래정지 등으로 OHLCV 조회가 반복 실패하는 종목을 자동 등록
        self._runtime_blacklist: set[str] = set()      # 세션 종료까지 스캔 제외
        self._data_error_count: dict[str, int] = {}    # ticker → 연속 DATA_ERROR 횟수

        # 거래소 주기 동기화 타임스탬프 (외부 매수 / 응답 유실 포지션 복구)
        self._last_exchange_sync: float = 0.0

        # 정상 종료 플래그
        self._running = False

        # 시나리오 정보 로그
        scenarios_info = ", ".join(
            f"{s.scenario_id}({len(s.tickers)}종목/{s.weight_pct:.0f}%/{s.budget_pct:.0f}%)"
            for s in self._scenarios
        )
        logger.info(
            f"시나리오({len(self._scenarios)}개): {scenarios_info} | "
            f"전체 종목: {len(self._active_tickers)}개 | "
            f"ATR사이징: {'ON' if config.USE_ATR_SIZING else 'OFF'} | "
            f"AutoTuner: {'ON' if config.USE_AUTO_TUNER else 'OFF'} | "
            f"호가WS: {'ON' if config.ORDERBOOK_WS_ENABLED else 'OFF'}"
        )

    # ─── 시작 / 종료 ─────────────────────────────────────────────────────────

    def start(self) -> None:
        """시스템 시작. 메인 스레드 블로킹."""
        self.state.load()
        self.state.reconcile_with_exchange(self.client)

        # ── 계좌 전체 보유 코인 자동 등록 ───────────────────────────────────
        # tickers=None → 시스템 외부에서 매수한 코인 포함 계좌 전체 동기화
        # 등록된 포지션은 첫 번째 시나리오 ID로 배정되어 orphan 루프에서 매도/손절 처리
        first_scenario_id = self._scenarios[0].scenario_id
        imported = self.state.sync_from_exchange(
            self.client,
            tickers=None,
            default_scenario_id=first_scenario_id,
        )

        # 새로 등록된 코인이 있으면 WebSocket 구독 목록과 active_tickers에 추가
        # (ws.start() 전에 추가해야 연결 시 전체 종목 구독)
        if imported:
            new_tickers = [t for t in imported if t not in self._active_tickers]
            if new_tickers:
                self._active_tickers.extend(new_tickers)
                self.ws._tickers = self._active_tickers  # start() 전이므로 직접 교체
                logger.info(
                    f"계좌 보유 종목 {len(new_tickers)}개 WebSocket 구독 추가: {new_tickers}"
                )

        # ── 블랙리스트 포지션 자동 제거 ─────────────────────────────────────
        # 블랙리스트에 추가된 종목이 positions.json에 남아있으면 가격 조회 실패 루프 발생
        # → 시작 시 자동으로 포지션 제거 (실제 보유 코인이면 업비트 앱에서 수동 매도 필요)
        blacklist_set = set(config.TICKER_BLACKLIST)
        blacklisted_pos = [
            pos for pos in self.state.all_positions()
            if pos.ticker in blacklist_set
        ]
        if blacklisted_pos:
            for pos in blacklisted_pos:
                self.state.remove_position(pos.ticker)
                logger.warning(
                    f"블랙리스트 포지션 제거: {pos.ticker} | "
                    f"buy_price={pos.buy_price:,.0f}원 | "
                    f"실제 보유 중이라면 업비트 앱에서 직접 매도하세요"
                )
            self.state.save()
            logger.info(f"블랙리스트 포지션 {len(blacklisted_pos)}개 제거 완료")

        # ── Orphan 포지션 시나리오 재할당 ───────────────────────────────────
        # positions.json의 scenario_id가 현재 활성 시나리오 목록에 없는 포지션을
        # 가장 적합한 활성 시나리오로 재할당 (UI에서 전략을 바꿨을 때 자동 정합)
        active_scenario_ids = {s.scenario_id for s in self._scenarios}
        # ticker → 해당 종목이 속한 첫 번째 시나리오 ID 매핑
        ticker_to_scen: dict[str, str] = {}
        for s in self._scenarios:
            for t in s.tickers:
                if t not in ticker_to_scen:
                    ticker_to_scen[t] = s.scenario_id

        reassigned: list[str] = []
        for pos in self.state.all_positions():
            if pos.scenario_id not in active_scenario_ids:
                # 해당 ticker가 어느 시나리오 종목 목록에 있으면 그 시나리오로,
                # 없으면 첫 번째 시나리오로 배정
                new_scen = ticker_to_scen.get(pos.ticker, first_scenario_id)
                old_scen = pos.scenario_id
                if self.state.update_position_scenario(pos.ticker, new_scen):
                    reassigned.append(f"{pos.ticker}({old_scen}→{new_scen})")

        if reassigned:
            self.state.save()
            logger.info(f"Orphan 포지션 시나리오 재할당 ({len(reassigned)}개): {reassigned}")
        else:
            logger.info("Orphan 포지션 없음 (모든 포지션 시나리오 정합)")

        current_equity = self.risk.get_total_equity()
        self._session_start_equity = current_equity   # Obsidian 일보용 기준 자산
        self.state.update_peak_equity(current_equity)
        self.state.save()

        # ── 세션 로깅 시작 ────────────────────────────────────────────────
        session_id = self.session_manager.start()
        self.trade_logger.set_session_id(session_id)

        self.ws.start()
        if self._ob_manager:
            self._ob_manager.start()
        self.scheduler.start()

        # WebSocket 안정화 대기 (최대 3초)
        for _ in range(6):
            if self.price_cache.connected_tickers_count > 0:
                break
            time.sleep(0.5)

        logger.info(
            f"=== 매매 루프 시작 | "
            f"WS={self.ws.is_connected} | "
            f"종목({len(self._active_tickers)}개): {self._active_tickers[:5]} | "
            f"equity={current_equity:,.0f}원 ==="
        )
        self._running = True
        self._run_buy_loop()

    def stop(self) -> None:
        """정상 종료"""
        logger.info("시스템 종료 요청...")
        self._running = False
        self.scheduler.stop()
        self.ws.stop()
        if self._ob_manager:
            self._ob_manager.stop()
        self.order_sm.reset()
        self.state.save()

        # ── 세션 로깅 종료 + 결과 수집 ──────────────────────────────────────
        try:
            equity = self.risk.get_total_equity()
            session_summary = self._build_obs_summary(equity)
            # 현재 포지션 스냅샷
            positions_snap = {
                p.ticker: {
                    "volume": p.volume,
                    "buy_price": p.buy_price,
                    "buy_time": p.buy_time,
                    "krw_spent": p.krw_spent,
                    "stop_loss_price": p.stop_loss_price,
                    "strategy_id": p.strategy_id,
                    "scenario_id": p.scenario_id,
                    "side": p.side,
                    "leverage": p.leverage,
                }
                for p in self.state.all_positions()
            }
            self.session_manager.finalize(
                summary=session_summary,
                positions_snapshot=positions_snap,
            )
            self.trade_logger.set_session_id(None)
        except Exception as e:
            logger.warning(f"세션 로깅 종료 실패: {e}")

        # ── Gemini 분석용 세션 로그 저장 ──────────────────────────────────────
        try:
            from logging_.session_log_writer import paper_trade_to_dict, save_session_log
            trade_dicts = [paper_trade_to_dict(t) for t in self._obs_session_trades]
            equity_now  = self.risk.get_total_equity()
            save_session_log(
                scenario_id=self.strategy.get_scenario_id(),
                trades=trade_dicts,
                summary=self._build_obs_summary(equity_now),
                is_paper=False,
            )
        except Exception as _e:
            logger.warning(f"Gemini 분석 로그 저장 실패: {_e}")

        # ── Obsidian 세션 종료 + 일보 저장 ──────────────────────────────────
        if self.obsidian_logger:
            try:
                equity  = self.risk.get_total_equity()
                summary = self._build_obs_summary(equity)
                self.obsidian_logger.log_session_end([{**summary, "is_paper": False}])
                self.obsidian_logger.log_daily_report(
                    self.strategy.get_scenario_id(),
                    summary,
                    self._obs_session_trades,
                    is_paper=False,
                )
                logger.info("Obsidian 세션 종료 + 일보 저장 완료")
            except Exception as e:
                logger.warning(f"Obsidian 종료 기록 실패: {e}")

        logger.info("시스템 종료 완료")

    # ─── 메인 매매 루프 ────────────────────────────────────────────────────────

    def _run_buy_loop(self) -> None:
        while self._running:
            loop_start = time.time()

            # ── 동적 종목 갱신 체크 ───────────────────────────────────────────
            if self._ticker_manager:
                # 단일전략 모드: DynamicTickerManager 사용 (기존 로직)
                try:
                    if self.universe_selector:
                        new_tickers, changed = self._ticker_manager.refresh_if_needed(
                            custom_fetcher=lambda n: self.universe_selector.select_top_n(n)
                        )
                    else:
                        new_tickers, changed = self._ticker_manager.refresh_if_needed()
                    if changed:
                        self._update_active_tickers(new_tickers)
                except Exception as e:
                    logger.warning(f"동적 종목 갱신 오류 (이전 종목 유지): {e}")
            elif self._multi_scenario_mode:
                # 멀티시나리오 모드(UI 실행): 주기적으로 거래량 상위 종목 직접 갱신
                now = time.time()
                refresh_sec = config.TICKER_REFRESH_HOURS * 3600
                if now - self._last_ticker_refresh >= refresh_sec:
                    self._refresh_scenario_tickers()
                    self._last_ticker_refresh = now

            # ── 거래소 포지션 주기 동기화 (외부 매수 / 응답 유실 포지션 복구) ────
            # 30분마다 실제 잔고를 조회해 positions.json에 없는 코인을 자동 등록
            now_ts = time.time()
            if now_ts - self._last_exchange_sync >= 1800:  # 30분
                try:
                    first_sid = self._scenarios[0].scenario_id
                    imported = self.state.sync_from_exchange(
                        self.client, tickers=None, default_scenario_id=first_sid
                    )
                    if imported:
                        new_t = [t for t in imported if t not in self._active_tickers]
                        if new_t:
                            self._active_tickers.extend(new_t)
                            logger.info(f"[주기동기화] 신규 종목 WebSocket 추가: {new_t}")
                except Exception as _e:
                    logger.warning(f"거래소 주기 동기화 오류 (무시): {_e}")
                finally:
                    self._last_exchange_sync = now_ts

            # ── 상태머신 타임아웃 처리 ───────────────────────────────────────
            self._handle_order_timeouts()

            # ── 보유 포지션 중 해당 시나리오 종목 리스트에서 빠진 것 처리 ────
            # 종목 갱신 후 ticker가 scenario.tickers에서 제거되어도 매도/손절 체크 보장
            for position in list(self.state.all_positions()):
                if not self._running:
                    break
                ticker = position.ticker
                for scenario in self._scenarios:
                    if scenario.scenario_id == position.scenario_id:
                        if ticker not in scenario.tickers:
                            # 보유 중이지만 현재 종목 리스트에 없음 → 매도/손절만 처리
                            try:
                                self._process_ticker(ticker, scenario)
                            except Exception as e:
                                logger.error(
                                    f"[orphan][{scenario.scenario_id}][{ticker}] 처리 오류: {e}",
                                    exc_info=True,
                                )
                                self._record_error()
                        break

            # ── 시나리오별 종목 처리 ─────────────────────────────────────────
            for scenario in self._scenarios:
                if not self._running:
                    break
                for ticker in scenario.tickers:
                    if not self._running:
                        break
                    try:
                        self._process_ticker(ticker, scenario)
                    except Exception as e:
                        logger.error(
                            f"[{scenario.scenario_id}][{ticker}] 처리 오류: {e}",
                            exc_info=True,
                        )
                        self._record_error()

            if self._is_error_threshold_exceeded():
                logger.critical("연속 에러 임계치 초과 - 시스템 종료")
                raise SystemExit(1)

            elapsed = time.time() - loop_start
            sleep_time = max(0.0, config.PRICE_CHECK_INTERVAL_SEC - elapsed)
            time.sleep(sleep_time)

    def _process_ticker(self, ticker: str, scenario: RealScenario | None = None) -> None:
        """단일 종목 처리: 가격 조회 → 상태머신 → 손절/매도신호 → 매수신호"""
        if scenario is None:
            scenario = self._scenarios[0]  # 하위호환

        # ── 런타임 블랙리스트 체크 (포지션 없는 종목만 스킵) ─────────────────
        if ticker in self._runtime_blacklist and not self.state.has_position(ticker):
            return

        # ── 매도 쿨다운 체크 (연속 실패 종목은 일정 시간 스킵) ────────────────
        cooldown_until = self._sell_cooldown.get(ticker)
        if cooldown_until:
            if time.time() < cooldown_until:
                return  # 쿨다운 중
            # 쿨다운 만료 → 초기화 후 재시도 허용
            del self._sell_cooldown[ticker]
            self._sell_fail_count.pop(ticker, None)
            logger.info(f"[쿨다운 해제] {ticker} 매도 재시도 허용")

        price = self._get_price(ticker)
        if price is None:
            return

        # 상태머신: pending 주문이 있으면 매매 로직 스킵
        if self.order_sm.has_pending_order(ticker):
            return

        with self._trade_lock:
            if self.state.has_position(ticker):
                position = self.state.get_position(ticker)

                # 이 시나리오가 소유한 포지션인지 확인
                if position.scenario_id != scenario.scenario_id:
                    return  # 다른 시나리오 소유 → 스킵

                # 1. 전략 고유 매도 신호
                sell_signal = scenario.strategy.should_sell_on_signal(ticker, price, position)
                if sell_signal.should_sell:
                    # ── 재진입(Re-entry) 체크 ──────────────────────────────
                    if self._should_reentry(sell_signal, position, price, scenario):
                        self._execute_reentry(position, price, sell_signal.reason)
                        return
                    self._execute_sell(position, price, reason=sell_signal.reason)
                    return

                # 2. 손절
                if self.risk.check_stop_loss(position, price):
                    self._execute_sell(position, price, reason="STOP_LOSS")
                    return

            else:
                # 3. 일일 매수 중복 체크 (스케줄 매도 전략만: 하루 1회)
                if scenario.strategy.requires_scheduled_sell() and self._already_bought_today(scenario, ticker):
                    return

                # 4. AutoTuner 필터 (활성 시)
                if self.auto_tuner and not self._check_auto_tuner(ticker):
                    return

                buy_signal = scenario.strategy.should_buy(ticker, price)
                self.trade_logger.log_signal(buy_signal)

                # ── 런타임 블랙리스트 자동 등록 (데이터 오류 반복 종목) ────────
                _DATA_ERROR_REASONS = ("DATA_ERROR", "DATA_INSUFFICIENT")
                _DATA_ERROR_LIMIT   = 5
                if buy_signal.reason in _DATA_ERROR_REASONS:
                    cnt = self._data_error_count.get(ticker, 0) + 1
                    self._data_error_count[ticker] = cnt
                    if cnt >= _DATA_ERROR_LIMIT:
                        self._runtime_blacklist.add(ticker)
                        self._data_error_count.pop(ticker, None)
                        logger.warning(
                            f"[런타임블랙리스트] {ticker} 데이터 오류 {cnt}회 연속 "
                            f"→ 세션 종료까지 스캔 제외 (상장폐지/거래정지 의심)"
                        )
                else:
                    self._data_error_count.pop(ticker, None)  # 정상 평가 시 카운터 리셋

                if buy_signal.should_buy:
                    # 시나리오 예산(equity × weight% × budget%) 기준으로 KRW 잔고 체크
                    equity = self.risk.get_total_equity()
                    min_budget = max(
                        config.MIN_ORDER_KRW,
                        int(equity * scenario.weight_pct / 100 * scenario.budget_pct / 100),
                    )
                    allowed, reason = self.risk.can_open_new_position(ticker, min_budget=min_budget)
                    if allowed:
                        self._execute_buy(ticker, price, buy_signal, scenario)
                    else:
                        logger.debug(f"매수 차단: {ticker} - {reason}")

    # ─── 멀티시나리오 종목 주기 갱신 ────────────────────────────────────────

    def _refresh_scenario_tickers(self) -> None:
        """
        멀티시나리오 모드 전용: 거래량 상위 종목을 시나리오별로 재선정.

        - 각 시나리오의 ticker_count 기준으로 top-N을 개별 조회
        - 실패 시 기존 ticker 목록 유지 (안전 폴백)
        - 종목 변경이 있으면 WebSocket 재구독
        - 보유 포지션 종목은 _run_buy_loop()의 orphan 패스가 별도 처리
        """
        logger.info("[Trader] 멀티시나리오 거래량 상위 종목 갱신 시작...")
        all_tickers: set[str] = set()

        for scenario in self._scenarios:
            n = scenario.ticker_count
            if n <= 0:
                all_tickers.update(scenario.tickers)
                continue
            try:
                new_tickers = MarketData.get_top_tickers_by_volume(n)
                # 블랙리스트 필터 (config 고정 + 런타임 자동 등록)
                combined_blacklist = set(config.TICKER_BLACKLIST) | self._runtime_blacklist
                new_tickers = [t for t in new_tickers if t not in combined_blacklist]
                new_tickers = new_tickers[:n]

                added   = set(new_tickers) - set(scenario.tickers)
                removed = set(scenario.tickers) - set(new_tickers)
                if added or removed:
                    logger.info(
                        f"[{scenario.scenario_id}] 종목 변경 | "
                        f"추가={sorted(added)[:3]} | 제거={sorted(removed)[:3]}"
                        + (f" 외 {max(0, len(added)-3)+max(0, len(removed)-3)}종목" if (len(added)+len(removed)) > 6 else "")
                    )
                else:
                    logger.debug(f"[{scenario.scenario_id}] 종목 변동 없음 ({n}개)")

                scenario.tickers = new_tickers
                all_tickers.update(new_tickers)
            except Exception as e:
                logger.warning(f"[{scenario.scenario_id}] 종목 갱신 실패 → 기존 유지: {e}")
                all_tickers.update(scenario.tickers)

        # WebSocket 재구독 (새 종목이 추가된 경우)
        new_active = list(all_tickers)
        if set(new_active) != set(self._active_tickers):
            self._update_active_tickers(new_active)
        else:
            self._active_tickers = new_active
            logger.info(f"[Trader] 종목 갱신 완료 (WebSocket 변경 없음) | 전체 {len(self._active_tickers)}개")

    # ─── 동적 종목 변경 ──────────────────────────────────────────────────────

    def _update_active_tickers(self, new_tickers: list[str]) -> None:
        """
        활성 종목 변경 + WebSocket 구독 갱신.
        종목이 바뀐 경우에만 호출됨 (refresh_if_needed가 changed=True 반환 시).
        WebSocket 재시작 시 price_cache는 공유 유지 (종목 변경 전 가격도 참조 가능).
        """
        old_tickers = self._active_tickers
        self._active_tickers = new_tickers

        logger.info(
            f"[Trader] 종목 변경 | {len(old_tickers)}→{len(new_tickers)}개 | "
            f"{old_tickers[:3]} → {new_tickers[:3]}"
        )

        # WebSocket 재시작: 기존 구독 종료 → 새 종목으로 재구독
        self.ws.stop()
        self.ws = WebSocketManager(self._active_tickers, self.price_cache)
        self.ws.start()

        # 호가 WebSocket도 재시작
        if self._ob_manager:
            self._ob_manager.stop()
            self._ob_manager = OrderbookManager(self._active_tickers, self.orderbook_cache)
            self._ob_manager.start()

        # WebSocket 안정화 대기 (최대 2초)
        for _ in range(4):
            if self.price_cache.connected_tickers_count > 0:
                break
            time.sleep(0.5)

        logger.info(f"[Trader] WebSocket 재시작 완료 | 구독 종목: {self._active_tickers[:5]}")

    # ─── 동적 N 변경 (외부에서 호출 가능) ────────────────────────────────────

    def set_top_n(self, n: int) -> None:
        """
        상위 N개 코인 수를 런타임에 변경.
        USE_DYNAMIC_TICKERS=True일 때만 유효.
        n은 10 단위 (10/20/30 … 100). 다음 refresh_if_needed() 호출 시 적용.
        """
        if self._ticker_manager is None:
            logger.warning("set_top_n: USE_DYNAMIC_TICKERS=False — 동적 종목 사용 안 함")
            return
        self._ticker_manager.n = n
        logger.info(f"[Trader] 상위 N 변경 → {self._ticker_manager.n}개 (다음 루프에서 갱신)")

    # ─── 재진입 (Re-entry) ──────────────────────────────────────────────────

    def _should_reentry(self, sell_signal, position, price: float,
                        scenario: RealScenario | None = None) -> bool:
        """
        재진입 조건 판단.

        조건:
          1. 현재 시나리오가 REENTRY_ENABLED_SCENARIOS에 포함될 것
          2. 매도 신호가 수익 구간에서 발생했을 것 (current_price > buy_price)
          3. 손절(STOP_LOSS)은 재진입 제외
        """
        scen_id = scenario.scenario_id if scenario else config.SELECTED_SCENARIO
        if scen_id not in config.REENTRY_ENABLED_SCENARIOS:
            return False
        if "STOP_LOSS" in sell_signal.reason:
            return False
        return price > position.buy_price

    def _execute_reentry(
        self,
        position,
        price: float,
        original_reason: str,
    ) -> None:
        """
        재진입 실행: 실제 매도 없이 기준가와 손절가만 갱신.

        - buy_price      → current price (새 기준 매수가)
        - stop_loss_price→ new_buy_price × (1 - STOP_LOSS_PCT)
        - krw_spent      → 그대로 유지 (원래 투자금 추적)
        """
        new_stop = price * (1 - config.STOP_LOSS_PCT)
        self.state.update_position_entry(position.ticker, price, new_stop)

        logger.info(
            f"[Re-entry] {position.ticker} | "
            f"기존 매수가={position.buy_price:,.0f} → 신규={price:,.0f} | "
            f"손절가={new_stop:,.0f} | 원인={original_reason}"
        )

        # 거래 로그: REENTRY 이벤트 기록
        self.trade_logger.log_trade(TradeRecord(
            ticker      = position.ticker,
            action      = "REENTRY",
            price       = price,
            volume      = position.volume,
            strategy_id = position.strategy_id,
            scenario_id = position.scenario_id,
            reason      = f"REENTRY({original_reason})",
            order_uuid  = position.order_uuid,
            stop_loss_price = new_stop,
        ))

    # ─── 일일 매수 추적 ──────────────────────────────────────────────────────

    def _already_bought_today(self, scenario: RealScenario, ticker: str) -> bool:
        """오늘 이미 매수한 종목인지 확인 (VB 전략: 1일 1매수)"""
        today = datetime.now(KST).date()
        return scenario.daily_buy_tracker.get(ticker) == today

    def _mark_bought_today(self, scenario: RealScenario, ticker: str) -> None:
        scenario.daily_buy_tracker[ticker] = datetime.now(KST).date()

    def reset_daily_tracker(self) -> None:
        """09:00 스케줄 매도 후 호출 → 새 거래일 시작"""
        for s in self._scenarios:
            s.daily_buy_tracker.clear()
        logger.info("일일 매수 추적기 초기화")

    # ─── 매수 실행 ────────────────────────────────────────────────────────────

    def _execute_buy(self, ticker: str, price: float, signal,
                     scenario: RealScenario | None = None) -> None:
        """주문 → 체결 확인(폴링) → 상태 업데이트"""
        if scenario is None:
            scenario = self._scenarios[0]

        # ── 예산 계산 (% 기반) ──────────────────────────────────────────────
        equity = self.risk.get_total_equity()
        scenario_equity = equity * scenario.weight_pct / 100
        sizing_sl_price = 0.0

        if self.position_sizer:
            try:
                atr, atr_pct, _ = self.market_data.compute_atr_pct(ticker)
                sl_mult = signal.metadata.get("sl_atr_mult", 1.5) if signal.metadata else 1.5
                sizing = self.position_sizer.calculate(
                    equity_krw=scenario_equity,
                    entry_price=price,
                    atr=atr,
                    sl_atr_mult=sl_mult,
                )
                if not sizing.valid:
                    logger.info(f"[PositionSizer] 진입 불가: {ticker} - {sizing.reason}")
                    return
                buy_amount = int(sizing.order_krw)
                sizing_sl_price = sizing.sl_price
                logger.debug(
                    f"[PositionSizer] {ticker}: order={buy_amount:,}원 "
                    f"SL={sizing.sl_price:,.0f} risk={sizing.risk_pct:.4f}"
                )
            except Exception as e:
                logger.warning(f"[PositionSizer] 계산 실패 → % 예산 사용: {ticker} - {e}")
                buy_amount = int(scenario_equity * scenario.budget_pct / 100)
        else:
            # % 기반 예산 계산
            buy_amount = int(scenario_equity * scenario.budget_pct / 100)

        buy_amount = min(buy_amount, config.MAX_ORDER_KRW)
        buy_amount = max(buy_amount, config.MIN_ORDER_KRW)

        logger.info(f"매수 시도 | {ticker} | price={price:,.0f} | amount={buy_amount:,}원 | reason={signal.reason}")

        # 상태머신: IDLE → ENTRY_PENDING
        sl_pct_for_sm = signal.metadata.get("stop_loss_pct", config.STOP_LOSS_PCT) if signal.metadata else config.STOP_LOSS_PCT
        self.order_sm.request_entry(
            ticker=ticker,
            order_uuid="",
            entry_price=price,
            sl_price=sizing_sl_price if sizing_sl_price > 0 else price * (1 - sl_pct_for_sm),
        )

        try:
            order: OrderResult = self.client.buy_market_order(ticker, buy_amount)
        except (OrderFailedError, InsufficientBalanceError) as e:
            logger.error(f"매수 실패: {ticker} - {e}")
            self.order_sm.cancel_entry(ticker, reason=str(e))
            self.trade_logger.log_trade(TradeRecord(
                ticker=ticker, action="BUY", price=price, volume=0,
                strategy_id=scenario.strategy_id,
                scenario_id=scenario.scenario_id,
                reason=signal.reason, order_uuid="FAILED",
                error=str(e),
            ))
            return

        if order.volume <= 0 or order.state == "cancel":
            logger.warning(f"매수 미체결/취소: {ticker} vol={order.volume} state={order.state}")
            self.order_sm.cancel_entry(ticker, reason="미체결/취소")
            return

        # 손절가 결정 우선순위:
        # 1) PositionSizer가 계산한 SL (ATR 기반)
        # 2) 전략 메타데이터 stop_loss_pct
        # 3) config.STOP_LOSS_PCT (기본 3%)
        if sizing_sl_price > 0:
            stop_loss_price = sizing_sl_price
        else:
            sl_pct = signal.metadata.get("stop_loss_pct", config.STOP_LOSS_PCT) if signal.metadata else config.STOP_LOSS_PCT
            stop_loss_price = order.avg_price * (1 - sl_pct)

        # 상태머신: 매수 체결 확인
        self.order_sm.confirm_entry(ticker, order.volume, order.avg_price)

        position = Position(
            ticker=ticker,
            volume=order.volume,
            buy_price=order.avg_price,
            buy_time=now_kst(),
            krw_spent=buy_amount,
            order_uuid=order.uuid,
            stop_loss_price=stop_loss_price,
            strategy_id=scenario.strategy_id,
            scenario_id=scenario.scenario_id,
        )

        self.state.add_position(position)
        self.state.save()
        self._mark_bought_today(scenario, ticker)

        equity = self.risk.get_total_equity()
        self.state.update_peak_equity(equity)

        # 옵시디언 기록 + 세션 누적
        try:
            from core.paper_account import PaperTrade as _PT
            _obs_buy = _PT(
                trade_no=len(self._obs_session_trades) + 1,
                timestamp=position.buy_time,
                account_id="REAL", scenario_id=self.strategy.get_scenario_id(),
                ticker=ticker, action="BUY",
                price=order.avg_price, volume=order.volume,
                amount_krw=buy_amount, fee=order.paid_fee,
                reason=signal.reason, balance_after=0.0,
                indicators=signal.metadata or {},
            )
            self._obs_session_trades.append(_obs_buy)
            if self.obsidian_logger:
                self.obsidian_logger.log_trade(
                    self.strategy.get_scenario_id(), _obs_buy, is_paper=False
                )
        except Exception:
            pass

        self.trade_logger.log_trade(TradeRecord(
            ticker=ticker,
            action="BUY",
            price=order.avg_price,
            volume=order.volume,
            strategy_id=position.strategy_id,
            scenario_id=position.scenario_id,
            reason=signal.reason,
            order_uuid=order.uuid,
            krw_amount=buy_amount,
            fee=round(order.paid_fee, 2) if order.paid_fee > 0 else None,
            stop_loss_price=stop_loss_price,
            total_equity=equity,
            metadata=signal.metadata,
        ))

    # ─── 매도 실행 ────────────────────────────────────────────────────────────

    def _execute_sell(self, position: Position, price: float, reason: str) -> None:
        """
        포지션 매도.
        매도 실패 시 포지션 유지 (자산 추적 보호).
        매도 성공 시에만 포지션 제거.
        """
        logger.info(f"매도 시도 | {position.ticker} | price={price:,.0f} | reason={reason}")

        # 상태머신: EXIT_PENDING 전이
        self.order_sm.request_exit(position.ticker, "", reason)

        order: OrderResult | None = None
        error_msg: str | None = None

        for attempt in range(3):
            try:
                order = self.client.sell_market_order(position.ticker, position.volume)
                if order.volume > 0 and order.state != "cancel":
                    break
                logger.warning(f"매도 미체결, 재시도 ({attempt+1}/3): {position.ticker}")
            except OrderFailedError as e:
                error_str = str(e)
                # ── 소액 포지션: 업비트 최소 주문금액(5,000원) 미달 ──────────────
                # 재시도해도 동일 오류 → 즉시 포지션 제거 후 반환
                if "under_min_total_market_ask" in error_str:
                    val = price * position.volume
                    logger.warning(
                        f"소액 포지션 매도 불가 (5,000원 미달 {val:.0f}원) → "
                        f"포지션 자동 제거: {position.ticker}"
                    )
                    self.order_sm.confirm_exit(position.ticker)
                    self.state.remove_position(position.ticker)
                    self.state.save()
                    self.trade_logger.log_trade(TradeRecord(
                        ticker=position.ticker, action="SELL", price=price,
                        volume=position.volume,
                        strategy_id=position.strategy_id,
                        scenario_id=position.scenario_id,
                        reason=f"{reason}(소액청산)",
                        order_uuid="UNDER_MIN_REMOVED",
                        error=None,
                    ))
                    return
                if attempt < 2:
                    logger.warning(f"매도 재시도 ({attempt+1}/3): {position.ticker} - {e}")
                    time.sleep(1.5 ** attempt)
                else:
                    logger.critical(f"매도 최종 실패: {position.ticker} - {e}")
                    error_msg = str(e)

        # 매도 실패 → 포지션 유지, 수동 확인 필요
        if order is None or order.volume <= 0:
            logger.critical(
                f"매도 실패 - 포지션 유지 | {position.ticker} | "
                f"수동 확인 필요 | error={error_msg}"
            )
            # 상태머신: 매도 취소 → IN_POSITION 복귀 (재시도 가능)
            self.order_sm.cancel_exit(position.ticker, reason=error_msg or "체결 실패")
            self.trade_logger.log_trade(TradeRecord(
                ticker=position.ticker, action="SELL", price=price,
                volume=position.volume,
                strategy_id=position.strategy_id,
                scenario_id=position.scenario_id,
                reason=reason, order_uuid="FAILED",
                error=error_msg or "체결 수량 0",
            ))
            # ── 연속 실패 카운터 → 쿨다운 ────────────────────────────────────
            _FAIL_LIMIT   = 3    # N회 연속 실패 시 쿨다운 진입
            _COOLDOWN_SEC = 300  # 5분 쿨다운
            fail = self._sell_fail_count.get(position.ticker, 0) + 1
            self._sell_fail_count[position.ticker] = fail
            if fail >= _FAIL_LIMIT:
                self._sell_cooldown[position.ticker] = time.time() + _COOLDOWN_SEC
                self._sell_fail_count.pop(position.ticker, None)
                logger.warning(
                    f"[쿨다운 진입] {position.ticker} 매도 {fail}회 연속 실패 "
                    f"→ {_COOLDOWN_SEC//60}분간 재시도 중단 (업비트 앱에서 수동 매도 권장)"
                )
            return

        # 손익 계산 (수수료 포함 실질 손익, side 대응)
        sell_price = order.avg_price if order.avg_price > 0 else price
        sell_gross  = sell_price * order.volume
        sell_fee    = order.paid_fee if order.paid_fee > 0 else sell_gross * config.FEE_RATE
        net_proceeds = sell_gross - sell_fee

        if position.side == "SHORT":
            # SHORT: 매도(진입) 시 받은 금액 - 매수(청산) 시 지불 금액
            pnl_krw = position.krw_spent - net_proceeds
        else:
            # LONG: 매도 수령금 - 매수 투자금
            pnl_krw = net_proceeds - position.krw_spent
        pnl_pct = pnl_krw / position.krw_spent if position.krw_spent > 0 else 0

        # 매도 성공 → 포지션 제거 + 상태머신 청산 완료
        self.order_sm.confirm_exit(position.ticker)
        self.state.remove_position(position.ticker)
        self.state.save()
        # 매도 성공 시 실패 카운터/쿨다운 초기화
        self._sell_fail_count.pop(position.ticker, None)
        self._sell_cooldown.pop(position.ticker, None)

        # 전략 내부 상태 정리 (peak, 타임컷 연장 등) — reason 전달로 쿨다운 등 후처리 가능
        for _s in self._scenarios:
            if _s.scenario_id == position.scenario_id:
                _s.strategy.on_position_closed(position.ticker, reason=reason)
                break

        equity = self.risk.get_total_equity()
        self.state.update_peak_equity(equity)
        self.state.save()

        # 옵시디언 기록 + 세션 누적
        try:
            from core.paper_account import PaperTrade as _PT
            _obs_sell = _PT(
                trade_no=len(self._obs_session_trades) + 1,
                timestamp=datetime.now(KST),
                account_id="REAL", scenario_id=position.scenario_id,
                ticker=position.ticker, action="SELL",
                price=sell_price, volume=order.volume,
                amount_krw=net_proceeds,   # 수수료 차감 후 실수령금
                fee=sell_fee, reason=reason,
                pnl=pnl_krw, pnl_pct=pnl_pct * 100,
                balance_after=0.0, indicators={},
            )
            self._obs_session_trades.append(_obs_sell)
            if self.obsidian_logger:
                self.obsidian_logger.log_trade(
                    position.scenario_id, _obs_sell, is_paper=False
                )
        except Exception:
            pass

        self.trade_logger.log_trade(TradeRecord(
            ticker=position.ticker,
            action="SELL",
            price=sell_price,
            volume=order.volume,
            strategy_id=position.strategy_id,
            scenario_id=position.scenario_id,
            reason=reason,
            order_uuid=order.uuid,
            krw_amount=round(net_proceeds, 0),
            fee=round(sell_fee, 2),
            pnl_krw=round(pnl_krw, 2),
            pnl_pct=round(pnl_pct, 6),
            total_equity=equity,
            error=error_msg,
        ))

    # ─── Obsidian 일보용 요약 빌더 ──────────────────────────────────────────

    def _build_obs_summary(self, equity: float) -> dict:
        """
        실거래 세션 집계를 dict로 반환 (log_session_end / log_daily_report용).
        _obs_session_trades (PaperTrade 리스트) 기반으로 계산.
        """
        sells = [t for t in self._obs_session_trades if t.action == "SELL"]
        wins  = [t for t in sells if t.pnl > 0]
        pnl   = equity - self._session_start_equity
        return {
            "scenario_id":     self.strategy.get_scenario_id(),
            "is_paper":        False,
            "initial_balance": self._session_start_equity,
            "current_equity":  equity,
            "total_pnl":       pnl,
            "total_pnl_pct":   (
                pnl / self._session_start_equity * 100
                if self._session_start_equity else 0.0
            ),
            "total_trades":    len(self._obs_session_trades),
            "sell_count":      len(sells),
            "win_rate":        len(wins) / len(sells) * 100 if sells else 0.0,
        }

    # ─── 스케줄러 호출: 전체 매도 ────────────────────────────────────────────

    def sell_all_positions(self, reason: str = "SCHEDULED_09H") -> None:
        """보유 포지션 전체 매도 (스케줄러 및 강제 청산용)"""
        with self._trade_lock:
            positions = self.state.all_positions()
            if not positions:
                logger.info("매도 대상 포지션 없음")
                return

            logger.info(f"전 포지션 매도 | 이유={reason} | 종목수={len(positions)}")
            for position in positions:
                price = self._get_price(position.ticker) or position.buy_price
                self._execute_sell(position, price, reason=reason)

        if reason == "SCHEDULED_09H":
            self.reset_daily_tracker()
            # 전략 내부 일별 상태 초기화 (peak, 타임컷 연장 등)
            for _s in self._scenarios:
                if _s.strategy.requires_scheduled_sell():
                    _s.strategy.reset_daily()

        # ── Obsidian 일보 (스케줄 매도 또는 강제 청산 후) ───────────────────
        if self.obsidian_logger:
            try:
                equity  = self.risk.get_total_equity()
                summary = self._build_obs_summary(equity)
                self.obsidian_logger.log_daily_report(
                    self.strategy.get_scenario_id(),
                    summary,
                    self._obs_session_trades,
                    is_paper=False,
                )
                logger.info(f"Obsidian 일보 저장 완료 (reason={reason})")
            except Exception as e:
                logger.warning(f"Obsidian 일보 실패: {e}")

    # ─── 가격 조회 (캐시 우선, REST 폴백) ────────────────────────────────────

    def _get_price(self, ticker: str) -> float | None:
        """PriceCache 우선, stale이면 REST 폴백, REST도 실패하면 stale 캐시 사용"""
        price = self.price_cache.get(ticker)
        if price and not self.price_cache.is_stale(ticker, config.WEBSOCKET_STALE_SEC):
            return price

        try:
            price = self.client.get_current_price(ticker)
            self.price_cache.update(ticker, price)
            return price
        except Exception as e:
            logger.warning(f"REST 가격 조회 실패: {ticker} - {e}")
            cached = self.price_cache.get(ticker)
            if cached:
                logger.debug(f"stale 캐시 가격 사용: {ticker} = {cached:,.0f}")
                return cached
            return None

    # ─── 에러 임계치 ─────────────────────────────────────────────────────────

    def _record_error(self) -> None:
        now = time.time()
        if now - self._error_window_start > 60:
            self._error_count = 0
            self._error_window_start = now
        self._error_count += 1

    def _is_error_threshold_exceeded(self) -> bool:
        return self._error_count > 10

    # ─── AutoTuner 필터 체크 ───────────────────────────────────────────────

    def _check_auto_tuner(self, ticker: str) -> bool:
        """
        AutoTuner 필터: fee_edge + spread 체크.
        True = 진입 허용, False = 진입 차단.
        """
        if not self.auto_tuner:
            return True

        try:
            atr, atr_pct, close = self.market_data.compute_atr_pct(ticker)
            spread_bps = self.orderbook_cache.get_spread_bps(ticker)

            metrics = SymbolMetrics(
                ticker=ticker,
                last_close=close,
                atr=atr,
                atr_pct=atr_pct,
                spread_bps=spread_bps,
            )

            params = self.auto_tuner.compute(
                self.strategy.get_scenario_id(), metrics
            )

            if not params.entry_allowed:
                logger.debug(
                    f"[AutoTuner] {ticker} 진입 차단 | "
                    f"fee_edge={params.fee_edge_ok} spread={params.spread_ok} "
                    f"ATR%={atr_pct:.4f} spread={spread_bps:.1f}bps"
                )
                return False

            return True
        except Exception as e:
            logger.debug(f"[AutoTuner] {ticker} 필터 계산 실패 → 허용: {e}")
            return True

    # ─── 상태머신 타임아웃 처리 ────────────────────────────────────────────

    def _handle_order_timeouts(self) -> None:
        """
        상태머신의 pending 주문 타임아웃 처리.
        ENTRY_PENDING 타임아웃 → cancel_entry
        EXIT_PENDING 타임아웃  → cancel_exit (IN_POSITION 복귀, 재시도 가능)
        """
        expired = self.order_sm.check_timeouts()
        for ticker, state in expired:
            if state == OrderState.ENTRY_PENDING:
                self.order_sm.cancel_entry(ticker, reason="TIMEOUT")
                logger.warning(f"[OrderSM] {ticker} 매수 타임아웃 → IDLE")
            elif state == OrderState.EXIT_PENDING:
                self.order_sm.cancel_exit(ticker, reason="TIMEOUT")
                logger.warning(f"[OrderSM] {ticker} 매도 타임아웃 → IN_POSITION (재시도 대기)")
