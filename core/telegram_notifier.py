from __future__ import annotations

import json
import logging
import threading
import urllib.parse
import urllib.request

import config

logger = logging.getLogger(__name__)

_AUTO_CHAT_ID: str | None = None
_AUTO_CHAT_LOOKUP_FAILED = False


def _fmt_price(price: float) -> str:
    if price >= 100:
        return f"{price:,.0f}"
    if price >= 1:
        return f"{price:,.3f}"
    return f"{price:,.6f}"


def _api_url(token: str, method: str) -> str:
    return f"https://api.telegram.org/bot{token}/{method}"


def _post_json(token: str, method: str, payload: dict) -> dict:
    data = urllib.parse.urlencode(payload).encode("utf-8")
    request = urllib.request.Request(
        _api_url(token, method),
        data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        return json.loads(response.read().decode("utf-8"))


def _get_json(token: str, method: str, query: dict | None = None) -> dict:
    url = _api_url(token, method)
    if query:
        url = f"{url}?{urllib.parse.urlencode(query)}"
    with urllib.request.urlopen(url, timeout=10) as response:
        return json.loads(response.read().decode("utf-8"))


def detect_chat_id_for_token(token: str) -> str:
    token = (token or "").strip()
    if not token:
        return ""

    try:
        payload = _get_json(token, "getUpdates", {"limit": 20, "timeout": 0})
    except Exception as exc:
        logger.warning(f"[Telegram] chat id 조회 실패: {exc}")
        return ""

    results = payload.get("result") or []
    for update in reversed(results):
        for key in ("message", "edited_message", "channel_post", "my_chat_member"):
            item = update.get(key)
            if not isinstance(item, dict):
                continue
            chat = item.get("chat")
            if isinstance(chat, dict) and chat.get("id") is not None:
                return str(chat["id"])
    return ""


def _resolve_chat_id() -> str:
    global _AUTO_CHAT_ID, _AUTO_CHAT_LOOKUP_FAILED

    explicit = (config.TELEGRAM_CHAT_ID or "").strip()
    if explicit:
        return explicit

    token = (config.TELEGRAM_BOT_TOKEN or "").strip()
    if not token:
        return ""

    if _AUTO_CHAT_ID:
        return _AUTO_CHAT_ID
    if _AUTO_CHAT_LOOKUP_FAILED:
        return ""

    detected = detect_chat_id_for_token(token)
    if detected:
        _AUTO_CHAT_ID = detected
        logger.info(f"[Telegram] 최근 대화 chat id 자동 감지: {detected}")
        return detected

    _AUTO_CHAT_LOOKUP_FAILED = True
    logger.warning(
        "[Telegram] chat id를 찾지 못했습니다. 봇에게 먼저 메시지를 보내거나 "
        "설정 탭에 chat id를 직접 입력하세요."
    )
    return ""


def is_enabled() -> bool:
    return bool(config.TELEGRAM_ENABLED and (config.TELEGRAM_BOT_TOKEN or "").strip())


def send_message(text: str) -> bool:
    if not is_enabled():
        return False

    token = (config.TELEGRAM_BOT_TOKEN or "").strip()
    chat_id = _resolve_chat_id()
    if not token or not chat_id:
        return False

    try:
        response = _post_json(
            token,
            "sendMessage",
            {
                "chat_id": chat_id,
                "text": text,
                "disable_web_page_preview": "true",
            },
        )
        if not response.get("ok"):
            logger.warning(f"[Telegram] 메시지 전송 실패: {response}")
            return False
        return True
    except Exception as exc:
        logger.warning(f"[Telegram] 메시지 전송 예외: {exc}")
        return False


def send_message_async(text: str) -> None:
    threading.Thread(
        target=send_message,
        args=(text,),
        daemon=True,
        name="TelegramSend",
    ).start()


def send_real_trade_close_notification(record) -> bool:
    if not config.TELEGRAM_NOTIFY_REAL_SELLS:
        return False

    metadata = record.metadata if isinstance(record.metadata, dict) else {}
    buy_price = float(metadata.get("buy_price") or 0.0)
    sell_price = float(metadata.get("sell_avg_price") or record.price or 0.0)
    pnl_krw = float(record.pnl_krw or 0.0)
    pnl_pct = float(record.pnl_pct or 0.0) * 100.0

    message = (
        "[실제거래 매도 완료]\n"
        f"- 전략: {record.scenario_id}\n"
        f"- 종목: {record.ticker}\n"
        f"- 매수가: {_fmt_price(buy_price)}\n"
        f"- 매도가: {_fmt_price(sell_price)}\n"
        f"- 실현손익: {pnl_krw:+,.0f}원\n"
        f"- 수익률: {pnl_pct:+.3f}%\n"
        f"- 사유: {record.reason}"
    )
    return send_message(message)


def send_real_trade_close_notification_async(record) -> None:
    threading.Thread(
        target=send_real_trade_close_notification,
        args=(record,),
        daemon=True,
        name="TelegramRealSell",
    ).start()


def send_real_stop_summary_notification(message: str) -> bool:
    if not config.TELEGRAM_NOTIFY_REAL_STOP_SUMMARY:
        return False
    payload = f"[실제거래 정지]\n{message}"
    return send_message(payload)


def send_real_stop_summary_notification_async(message: str) -> None:
    threading.Thread(
        target=send_real_stop_summary_notification,
        args=(message,),
        daemon=True,
        name="TelegramRealStopSummary",
    ).start()
