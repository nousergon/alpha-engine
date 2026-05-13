"""
Telegram trade notification sender — daemon-side structured-message formatters.

Sits on top of ``alpha_engine_lib.telegram.send_message`` (lib v0.14.0+), which
handles token/chat_id resolution, markdown escape, retry/timeout, and
fire-and-forget bool-return semantics. This module contributes daemon-specific
*message formatting* — emoji + structured trade/status templates — and
nothing else.

Setup (one-time):
  1. Message @BotFather on Telegram → /newbot → save the bot token
  2. Set ``TELEGRAM_BOT_TOKEN`` in SSM at ``/alpha-engine/TELEGRAM_BOT_TOKEN``
  3. Message the bot, then call getUpdates to get your chat_id
  4. Set ``TELEGRAM_CHAT_ID`` in SSM at ``/alpha-engine/TELEGRAM_CHAT_ID``

Migration arc: ROADMAP L1067 PR 2a (2026-05-13). Previously this module owned
the primitive send path inline; the surveillance Lambda arc required a second
producer, so the primitive was consolidated into ``alpha_engine_lib.telegram``
to prevent the "two writers diverged silently" antipattern.
"""

from __future__ import annotations

import logging

from alpha_engine_lib.telegram import send_message

logger = logging.getLogger(__name__)


def send_trade_alert(
    action: str,
    ticker: str,
    shares: int,
    price: float,
    trigger: str = "",
    source: str = "daemon",
) -> bool:
    """Send a Telegram push notification for a trade execution.

    Returns True if sent successfully, False otherwise (missing secrets,
    network error, non-200 response — all swallowed by the lib substrate).
    """
    emoji = {"BUY": "\U0001f7e2", "SELL": "\U0001f534", "REDUCE": "\U0001f7e1"}.get(action, "⚪")
    msg = (
        f"{emoji} *{action} {ticker}*\n"
        f"Shares: {shares} @ ${price:.2f}\n"
        f"Trigger: {trigger}\n"
        f"Source: {source}"
    )

    ok = send_message(msg)
    if ok:
        logger.info("Telegram alert sent: %s %s", action, ticker)
    else:
        logger.warning("Telegram trade alert failed for %s %s", action, ticker)
    return ok


def send_daemon_status(message: str) -> bool:
    """Send a general status message (daemon start/stop, errors, IB events)."""
    return send_message(message)
