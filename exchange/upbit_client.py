import time
import logging
import pyupbit

import config
from exchange.base_client import BaseExchangeClient, OrderResult  # noqa: F401 — re-export

logger = logging.getLogger(__name__)


# ─── 커스텀 예외 ──────────────────────────────────────────────────────────────

class UpbitTradingError(Exception):
    """업비트 거래 기반 예외"""

class InsufficientBalanceError(UpbitTradingError):
    """잔고 부족"""

class RateLimitError(UpbitTradingError):
    """API 레이트 리밋 초과"""

class OrderFailedError(UpbitTradingError):
    """주문 실패"""

class DataFetchError(UpbitTradingError):
    """시세 데이터 조회 실패"""


# ─── 업비트 클라이언트 ─────────────────────────────────────────────────────────

class UpbitClient(BaseExchangeClient):
    """
    pyupbit 래퍼.
    - 모든 API 호출에 지수 백오프 재시도 적용
    - 주문 후 체결 확인 폴링 (실체결 수량/가격 보장)
    - 최소 주문 금액 검증
    - 명확한 예외 계층 제공
    """

    def __init__(self, access_key: str, secret_key: str) -> None:
        if not access_key or not secret_key:
            raise ValueError("UPBIT_ACCESS_KEY / UPBIT_SECRET_KEY 환경변수가 설정되지 않았습니다.")
        self._upbit = pyupbit.Upbit(access_key, secret_key)
        logger.info("UpbitClient 초기화 완료")

    # ─── 잔고 조회 ────────────────────────────────────────────────────────────

    def get_balance(self, ticker: str) -> float:
        """
        잔고 조회.
        ticker='KRW' → 원화 잔고
        ticker='KRW-BTC' → BTC 수량
        """
        def _call():
            balance = self._upbit.get_balance(ticker)
            if balance is None:
                raise DataFetchError(f"잔고 조회 실패: {ticker}")
            return float(balance)

        return self._retry(_call)

    def get_balances(self) -> list:
        """전체 보유 자산 목록 조회"""
        def _call():
            result = self._upbit.get_balances()
            if result is None:
                raise DataFetchError("전체 잔고 조회 실패")
            return result

        return self._retry(_call)

    # ─── 현재가 조회 (REST) ───────────────────────────────────────────────────

    def get_current_price(self, ticker: str) -> float:
        """REST API로 현재가 조회 (WebSocket 폴백용)"""
        def _call():
            price = pyupbit.get_current_price(ticker)
            if price is None:
                raise DataFetchError(f"현재가 조회 실패: {ticker}")
            return float(price)

        return self._retry(_call)

    # ─── 주문 ─────────────────────────────────────────────────────────────────

    def buy_market_order(self, ticker: str, krw_amount: int) -> OrderResult:
        """
        시장가 매수 + 체결 확인 폴링.
        krw_amount: 투자 원화 금액
        반환: 체결 확인된 OrderResult
        """
        if krw_amount < config.MIN_ORDER_KRW:
            raise OrderFailedError(
                f"최소 주문 금액 미달: {krw_amount:,}원 (최소: {config.MIN_ORDER_KRW:,}원)"
            )

        krw_balance = self.get_balance("KRW")
        if krw_balance < krw_amount:
            raise InsufficientBalanceError(
                f"잔고 부족: 필요={krw_amount:,}원, 보유={krw_balance:,.0f}원"
            )

        # 수수료 여유분 (잔고 전액 사용 시 수수료로 실패 방지)
        actual_amount = krw_amount * (1 - config.FEE_RATE)

        def _call():
            result = self._upbit.buy_market_order(ticker, actual_amount)
            return self._validate_order_response(result, ticker)

        raw_order = self._retry(_call, max_retries=2)
        order_uuid = raw_order.get("uuid", "")
        logger.info(f"매수 주문 접수 | {ticker} | {krw_amount:,}원 | uuid={order_uuid}")

        return self._wait_for_fill(order_uuid, ticker, "bid")

    def sell_market_order(self, ticker: str, volume: float) -> OrderResult:
        """
        시장가 매도 + 체결 확인 폴링.
        volume: 매도 수량
        반환: 체결 확인된 OrderResult
        """
        def _call():
            result = self._upbit.sell_market_order(ticker, volume)
            return self._validate_order_response(result, ticker)

        raw_order = self._retry(_call, max_retries=2)
        order_uuid = raw_order.get("uuid", "")
        logger.info(f"매도 주문 접수 | {ticker} | volume={volume} | uuid={order_uuid}")

        return self._wait_for_fill(order_uuid, ticker, "ask")

    def get_order(self, uuid: str) -> dict:
        """주문 상태 조회"""
        def _call():
            result = self._upbit.get_order(uuid)
            if result is None:
                raise DataFetchError(f"주문 조회 실패: {uuid}")
            return result

        return self._retry(_call)

    # ─── 체결 확인 폴링 ───────────────────────────────────────────────────────

    def _wait_for_fill(self, order_uuid: str, ticker: str, side: str) -> OrderResult:
        """
        주문 체결 완료까지 폴링.
        state='done'/'cancel' 이 될 때까지 반복 조회.
        타임아웃 초과 시에도 현재까지의 체결 정보 반환.
        """
        deadline = time.time() + config.ORDER_CONFIRM_TIMEOUT_SEC

        while time.time() < deadline:
            try:
                order_info = self.get_order(order_uuid)
            except Exception as e:
                logger.warning(f"체결 확인 조회 실패, 재시도: {e}")
                time.sleep(config.ORDER_CONFIRM_POLL_SEC)
                continue

            state = order_info.get("state", "")
            if state in ("done", "cancel"):
                result = self._parse_order_result(order_info, ticker, side)
                logger.info(
                    f"주문 체결 완료 | {ticker} | {side} | "
                    f"vol={result.volume:.8f} | avg_price={result.avg_price:,.0f} | "
                    f"state={result.state}"
                )
                return result

            time.sleep(config.ORDER_CONFIRM_POLL_SEC)

        # 타임아웃
        logger.warning(f"체결 확인 타임아웃 ({config.ORDER_CONFIRM_TIMEOUT_SEC}초): {order_uuid}")
        try:
            order_info = self.get_order(order_uuid)
            return self._parse_order_result(order_info, ticker, side)
        except Exception:
            raise OrderFailedError(f"체결 확인 최종 실패: {order_uuid}")

    def _parse_order_result(self, order_info: dict, ticker: str, side: str) -> OrderResult:
        """주문 조회 응답을 OrderResult로 변환. trades에서 평균가 계산."""
        executed_volume = float(order_info.get("executed_volume", 0))
        paid_fee = float(order_info.get("paid_fee", 0))

        # trades 배열에서 실제 평균 체결가 계산
        trades = order_info.get("trades", [])
        if trades:
            total_funds = sum(float(t.get("funds", 0)) for t in trades)
            total_vol = sum(float(t.get("volume", 0)) for t in trades)
            avg_price = total_funds / total_vol if total_vol > 0 else 0
        else:
            # trades가 없는 경우 (미체결 또는 즉시 체결)
            price_val = order_info.get("price") or order_info.get("avg_price")
            avg_price = float(price_val) if price_val else 0

            # 매수 시 price=총투자금, executed_volume=체결수량으로 역산
            if avg_price == 0 and executed_volume > 0:
                locked = float(order_info.get("locked", 0))
                if side == "bid" and locked > 0:
                    avg_price = locked / executed_volume
                else:
                    try:
                        avg_price = self.get_current_price(ticker)
                    except Exception:
                        pass

        return OrderResult(
            uuid=order_info.get("uuid", ""),
            ticker=ticker,
            side=side,
            volume=executed_volume,
            avg_price=avg_price,
            paid_fee=paid_fee,
            state=order_info.get("state", "unknown"),
        )

    # ─── 응답 검증 ────────────────────────────────────────────────────────────

    def _validate_order_response(self, result, ticker: str) -> dict:
        """주문 API 응답 유효성 검증"""
        if result is None:
            raise OrderFailedError(f"주문 응답 없음: {ticker}")
        if isinstance(result, dict) and result.get("error"):
            err = result["error"]
            err_str = str(err)
            if "too_many_requests" in err_str.lower():
                raise RateLimitError(err_str)
            if "insufficient" in err_str.lower():
                raise InsufficientBalanceError(err_str)
            raise OrderFailedError(err_str)
        return result

    # ─── 주문 가능 정보 조회 (orders/chance) ───────────────────────────────────

    def get_order_chance(self, ticker: str) -> dict:
        """
        주문 가능 정보 조회 (GET /v1/orders/chance).
        반환: {
          "bid_fee": 0.0005,      # 매수 수수료율
          "ask_fee": 0.0005,      # 매도 수수료율
          "round_fee": 0.001,     # 왕복 수수료율
          "maker_bid_fee": ...,
          "maker_ask_fee": ...,
          "min_total": 5000.0,    # 최소 주문 총액 (KRW)
          "balance_krw": ...,     # KRW 잔고
          "balance_coin": ...,    # 해당 코인 잔고
        }
        """
        def _call():
            result = self._upbit.get_chance(ticker)
            if result is None:
                raise DataFetchError(f"주문 가능 정보 조회 실패: {ticker}")
            return result

        raw = self._retry(_call)

        bid_fee = float(raw.get("bid_fee", 0.0005))
        ask_fee = float(raw.get("ask_fee", 0.0005))

        # 잔고 파싱
        bid_account = raw.get("bid_account", {})
        ask_account = raw.get("ask_account", {})

        return {
            "bid_fee":        bid_fee,
            "ask_fee":        ask_fee,
            "round_fee":      bid_fee + ask_fee,
            "maker_bid_fee":  float(raw.get("maker_bid_fee", bid_fee)),
            "maker_ask_fee":  float(raw.get("maker_ask_fee", ask_fee)),
            "min_total":      float(raw.get("market", {}).get("bid", {}).get("min_total", 5000)),
            "balance_krw":    float(bid_account.get("balance", 0)),
            "balance_coin":   float(ask_account.get("balance", 0)),
        }

    # ─── 내부 유틸 ────────────────────────────────────────────────────────────

    def _retry(self, fn, max_retries: int = 3, backoff_base: float = 1.5):
        """
        지수 백오프 재시도.
        InsufficientBalanceError, ValueError 는 재시도 없이 즉시 전파.
        """
        last_exc = None
        for attempt in range(max_retries):
            try:
                return fn()
            except (InsufficientBalanceError, ValueError):
                raise
            except RateLimitError as e:
                wait = backoff_base ** attempt * 2
                logger.warning(f"레이트 리밋 ({attempt+1}/{max_retries}), {wait:.1f}초 대기: {e}")
                time.sleep(wait)
                last_exc = e
            except Exception as e:
                if attempt < max_retries - 1:
                    wait = backoff_base ** attempt
                    logger.warning(f"API 오류 재시도 ({attempt+1}/{max_retries}), {wait:.1f}초 대기: {e}")
                    time.sleep(wait)
                last_exc = e

        raise last_exc
