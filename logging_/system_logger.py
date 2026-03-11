import logging
import logging.handlers
import os
from datetime import datetime

import config
from logging_.log_context import AllowTradeModesFilter, InjectTradeModeFilter


_FMT = "%(asctime)s [%(levelname)-8s] %(name)s | %(message)s"
_DATEFMT = "%Y-%m-%d %H:%M:%S"


def _has_handler(root: logging.Logger, name: str) -> bool:
    return any(getattr(handler, "name", "") == name for handler in root.handlers)


def _build_timed_handler(
    path: str,
    level: int,
    name: str,
    mode_filter: logging.Filter | None = None,
) -> logging.Handler:
    handler = logging.handlers.TimedRotatingFileHandler(
        filename=path,
        when="midnight",
        interval=1,
        backupCount=30,
        encoding="utf-8",
    )
    handler.name = name
    handler.setLevel(level)
    handler.setFormatter(logging.Formatter(fmt=_FMT, datefmt=_DATEFMT))
    handler.addFilter(InjectTradeModeFilter())
    if mode_filter is not None:
        handler.addFilter(mode_filter)
    return handler


def setup_logging() -> None:
    """Configure shared app/system logs plus separate real/paper files."""
    for path in (
        config.SYSTEM_LOG_DIR,
        config.REAL_LOG_DIR,
        config.PAPER_LOG_DIR,
    ):
        os.makedirs(path, exist_ok=True)

    stamp = datetime.now().strftime("%Y-%m-%d")
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)

    if not _has_handler(root, "console"):
        console = logging.StreamHandler()
        console.name = "console"
        console.setLevel(logging.INFO)
        console.setFormatter(logging.Formatter(fmt=_FMT, datefmt=_DATEFMT))
        console.addFilter(InjectTradeModeFilter())
        root.addHandler(console)

    if not _has_handler(root, "system_file"):
        system_path = os.path.join(config.SYSTEM_LOG_DIR, f"app_{stamp}.log")
        root.addHandler(_build_timed_handler(system_path, logging.DEBUG, "system_file"))

    if not _has_handler(root, "real_file"):
        real_path = os.path.join(config.REAL_LOG_DIR, f"real_{stamp}.log")
        root.addHandler(
            _build_timed_handler(
                real_path,
                logging.DEBUG,
                "real_file",
                AllowTradeModesFilter("real"),
            )
        )

    if not _has_handler(root, "paper_file"):
        paper_path = os.path.join(config.PAPER_LOG_DIR, f"paper_{stamp}.log")
        root.addHandler(
            _build_timed_handler(
                paper_path,
                logging.DEBUG,
                "paper_file",
                AllowTradeModesFilter("paper"),
            )
        )

    logging.getLogger(__name__).info("로깅 시스템 초기화 완료")
