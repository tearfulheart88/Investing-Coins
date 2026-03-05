import logging
import logging.handlers
import os
from datetime import datetime
import config


def setup_logging() -> None:
    """
    루트 로거 설정.
    - 콘솔: INFO 이상 출력
    - 파일: DEBUG 이상, 날짜별 로테이팅 (자정 교체, 30일 보관)
    """
    os.makedirs(config.SYSTEM_LOG_DIR, exist_ok=True)

    log_path = os.path.join(
        config.SYSTEM_LOG_DIR,
        f"app_{datetime.now().strftime('%Y-%m-%d')}.log"
    )

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)

    # 이미 핸들러가 있으면 중복 추가 방지
    if root.handlers:
        return

    fmt = logging.Formatter(
        fmt="%(asctime)s [%(levelname)-8s] %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # 콘솔 핸들러
    console = logging.StreamHandler()
    console.setLevel(logging.INFO)
    console.setFormatter(fmt)
    root.addHandler(console)

    # 파일 핸들러 (자정 로테이팅)
    file_handler = logging.handlers.TimedRotatingFileHandler(
        filename=log_path,
        when="midnight",
        interval=1,
        backupCount=30,
        encoding="utf-8",
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(fmt)
    root.addHandler(file_handler)

    logging.info("로깅 시스템 초기화 완료")
