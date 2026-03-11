import logging
import threading


_thread_state = threading.local()


def set_log_mode(mode: str | None) -> None:
    _thread_state.trade_mode = mode


def get_log_mode() -> str | None:
    return getattr(_thread_state, "trade_mode", None)


def clear_log_mode() -> None:
    if hasattr(_thread_state, "trade_mode"):
        delattr(_thread_state, "trade_mode")


class InjectTradeModeFilter(logging.Filter):
    """Fill missing trade_mode so formatters/filters can rely on it."""

    def filter(self, record: logging.LogRecord) -> bool:
        mode = getattr(record, "trade_mode", None) or get_log_mode() or "system"
        record.trade_mode = mode
        return True


class AllowTradeModesFilter(logging.Filter):
    """Allow only selected trade_mode values."""

    def __init__(self, *allowed_modes: str) -> None:
        super().__init__()
        self._allowed_modes = set(allowed_modes)

    def filter(self, record: logging.LogRecord) -> bool:
        mode = getattr(record, "trade_mode", None) or get_log_mode() or "system"
        record.trade_mode = mode
        return mode in self._allowed_modes
