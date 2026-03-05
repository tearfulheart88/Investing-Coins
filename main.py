"""
Upbit Auto-Trading System
진입점. SIGINT/SIGTERM 신호 수신 시 정상 종료.

실행:
  python main.py

종료:
  Ctrl+C
"""
import os
import sys
import signal
import logging

from logging_.system_logger import setup_logging
import config
from core.trader import Trader

logger = logging.getLogger(__name__)


def validate_config() -> None:
    """시작 전 필수 설정값 검증. 문제 시 즉시 종료."""
    errors = []

    if not config.ACCESS_KEY:
        errors.append("UPBIT_ACCESS_KEY 환경변수 미설정")
    if not config.SECRET_KEY:
        errors.append("UPBIT_SECRET_KEY 환경변수 미설정")
    if not config.TICKERS:
        errors.append("TICKERS가 비어 있음")
    if config.BUDGET_PER_TRADE < config.MIN_ORDER_KRW:
        errors.append(
            f"BUDGET_PER_TRADE({config.BUDGET_PER_TRADE:,}원) < "
            f"MIN_ORDER_KRW({config.MIN_ORDER_KRW:,}원)"
        )
    if not (0 < config.STOP_LOSS_PCT < 1):
        errors.append(f"STOP_LOSS_PCT 범위 오류: {config.STOP_LOSS_PCT} (0~1)")
    if not (0 < config.MAX_DRAWDOWN_PCT < 1):
        errors.append(f"MAX_DRAWDOWN_PCT 범위 오류: {config.MAX_DRAWDOWN_PCT} (0~1)")

    if errors:
        for e in errors:
            logger.critical(f"설정 오류: {e}")
        sys.exit(1)


def main() -> None:
    setup_logging()

    logger.info("=" * 50)
    logger.info("Upbit Auto-Trading System 시작")
    logger.info("=" * 50)

    validate_config()

    trader = Trader()

    # 종료 핸들러
    def shutdown(signum, frame):
        logger.info(f"종료 시그널 수신 ({signum}), 정상 종료 중...")
        trader.stop()
        sys.exit(0)

    # SIGINT(Ctrl+C)는 모든 OS에서 지원
    signal.signal(signal.SIGINT, shutdown)

    # SIGTERM은 Unix 전용 — Windows에서는 무시
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, shutdown)

    # Windows: Ctrl+C 외에 콘솔 닫기/종료 이벤트 처리
    if os.name == "nt":
        try:
            import win32api  # type: ignore
            win32api.SetConsoleCtrlHandler(lambda _: (trader.stop(), True)[-1], True)
        except ImportError:
            pass  # pywin32 미설치 시 Ctrl+C만 지원

    try:
        trader.start()  # 블로킹 루프
    except SystemExit as e:
        logger.critical(f"시스템 종료: {e}")
        trader.stop()
        sys.exit(int(str(e)))
    except Exception as e:
        logger.critical(f"예상치 못한 오류로 종료: {e}", exc_info=True)
        trader.stop()
        sys.exit(1)


if __name__ == "__main__":
    main()
