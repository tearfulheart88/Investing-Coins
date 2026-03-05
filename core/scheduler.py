import logging
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from zoneinfo import ZoneInfo
import config

logger = logging.getLogger(__name__)

KST = ZoneInfo("Asia/Seoul")


class TradingScheduler:
    """
    APScheduler 기반 거래 스케줄러.

    등록 Job:
      1. [09:00 KST] 스케줄 매도: strategy.requires_scheduled_sell() True인 경우만 실행
      2. [09:01 KST] OHLCV 캐시 무효화: 신규 캔들 데이터 반영
      3. [매 1시간] 포트폴리오 equity 스냅샷 로깅

    timezone=Asia/Seoul 고정 → 서머타임 이슈 없음 (KST는 DST 없음)
    """

    def __init__(self, trader, market_data, trade_logger) -> None:
        self._trader = trader
        self._market_data = market_data
        self._trade_logger = trade_logger
        self._scheduler = BackgroundScheduler(timezone=KST)

    def start(self) -> None:
        self._register_jobs()
        self._scheduler.start()
        logger.info(
            f"스케줄러 시작 | "
            f"매도: {config.SELL_HOUR_KST:02d}:{config.SELL_MINUTE_KST:02d} KST"
        )

    def stop(self) -> None:
        self._scheduler.shutdown(wait=False)
        logger.info("스케줄러 종료")

    # ─── Job 등록 ─────────────────────────────────────────────────────────────

    def _register_jobs(self) -> None:
        # Job 1: 09:00 스케줄 매도
        self._scheduler.add_job(
            self._scheduled_sell_job,
            CronTrigger(
                hour=config.SELL_HOUR_KST,
                minute=config.SELL_MINUTE_KST,
                timezone=KST,
            ),
            id="scheduled_sell",
            name="09:00 스케줄 매도",
            misfire_grace_time=60,
        )

        # Job 2: 매도 1분 후 OHLCV 캐시 무효화
        # minute overflow 방지: 59분 → (hour+1, 0분)
        cache_minute = config.SELL_MINUTE_KST + 1
        cache_hour = config.SELL_HOUR_KST
        if cache_minute >= 60:
            cache_minute = 0
            cache_hour = (cache_hour + 1) % 24

        self._scheduler.add_job(
            self._invalidate_cache_job,
            CronTrigger(
                hour=cache_hour,
                minute=cache_minute,
                timezone=KST,
            ),
            id="invalidate_cache",
            name="OHLCV 캐시 무효화",
            misfire_grace_time=120,
        )

        # Job 3: 매 1시간 equity 스냅샷
        self._scheduler.add_job(
            self._equity_snapshot_job,
            IntervalTrigger(minutes=60),
            id="equity_snapshot",
            name="포트폴리오 스냅샷",
        )

    # ─── Job 구현 ─────────────────────────────────────────────────────────────

    def _scheduled_sell_job(self) -> None:
        """전략이 requires_scheduled_sell()이면 전 포지션 매도"""
        try:
            strategy = self._trader.strategy
            if not strategy.requires_scheduled_sell():
                logger.info(
                    f"[{strategy.get_scenario_id()}] requires_scheduled_sell=False → "
                    "스케줄 매도 건너뜀"
                )
                return

            logger.info("=== 09:00 스케줄 매도 시작 ===")
            self._trader.sell_all_positions(reason="SCHEDULED_09H")
            logger.info("=== 09:00 스케줄 매도 완료 ===")
        except Exception as e:
            logger.critical(f"스케줄 매도 Job 예외: {e}", exc_info=True)

    def _invalidate_cache_job(self) -> None:
        """신규 일봉 데이터 반영을 위해 OHLCV 캐시 초기화"""
        try:
            self._market_data.invalidate_cache()
            logger.info("OHLCV 캐시 무효화 완료 (새 캔들 반영)")
        except Exception as e:
            logger.error(f"캐시 무효화 Job 예외: {e}", exc_info=True)

    def _equity_snapshot_job(self) -> None:
        """포트폴리오 평가금액 주기적 로깅"""
        try:
            equity = self._trader.risk.get_total_equity()
            self._trader.state.update_peak_equity(equity)
            self._trader.state.save()
            self._trade_logger.log_equity_snapshot(
                equity, self._trader.state.peak_equity
            )
        except Exception as e:
            logger.warning(f"Equity 스냅샷 실패: {e}")
