"""Bot service entry point.

Starts the Telegram bot (polling) and the Redis pub/sub thread.
"""
from __future__ import annotations

import asyncio
import logging
import os
import threading

from dotenv import load_dotenv
load_dotenv()

from telegram.ext import (
    Application,
    CallbackQueryHandler,
    ChatMemberHandler,
    CommandHandler,
    MessageHandler,
    filters,
)

from .handlers.admin import cmd_broadcast, cmd_users, cmd_whitelist
from .handlers.analyze import cb_analyze_media, cb_noop
from .handlers.translate import handle_direct_text
from .handlers.chats import cb_chat_action, cb_link_chat, cmd_add, cmd_chats, cmd_done
from .handlers.groups import handle_my_chat_member
from .onboarding.wizard import cb_bot_added, cb_connect_wa, cb_group_created, cmd_start, handle_webapp_data
from .redis_sub import redis_subscriber_loop, set_bot_app, set_event_loop

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)


def main() -> None:
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not set")

    async def post_init(application):
        """Capture running event loop and start Redis subscriber thread."""
        set_event_loop(asyncio.get_running_loop())
        t = threading.Thread(target=redis_subscriber_loop, daemon=True)
        t.start()
        logger.info("Redis subscriber thread started")

    app = Application.builder().token(token).post_init(post_init).build()

    # Inject bot reference into redis_sub module
    set_bot_app(app)

    # ── Onboarding ────────────────────────────────────────
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CallbackQueryHandler(cb_connect_wa, pattern="^onboarding:connect_wa$"))
    app.add_handler(CallbackQueryHandler(cb_group_created, pattern="^onboarding:group_created$"))
    app.add_handler(CallbackQueryHandler(cb_bot_added, pattern="^onboarding:bot_added$"))

    # ── WebApp data (Mini App sends WA group selection) ──
    app.add_handler(MessageHandler(filters.StatusUpdate.WEB_APP_DATA, handle_webapp_data))

    # ── Chat management ───────────────────────────────────
    app.add_handler(CommandHandler("chats", cmd_chats))
    app.add_handler(CommandHandler("add", cmd_add))
    app.add_handler(CommandHandler("done", cmd_done))
    app.add_handler(CallbackQueryHandler(cb_link_chat, pattern=r"^link:"))
    app.add_handler(CallbackQueryHandler(cb_chat_action, pattern=r"^chat:(pause|resume):"))

    # ── Group tracking (my_chat_member) ───────────────────
    app.add_handler(ChatMemberHandler(handle_my_chat_member, ChatMemberHandler.MY_CHAT_MEMBER))

    # ── Media analysis ─────────────────────────────────────
    app.add_handler(CallbackQueryHandler(cb_analyze_media, pattern=r"^analyze:\d+$"))
    app.add_handler(CallbackQueryHandler(cb_noop, pattern=r"^noop$"))

    # ── Admin ─────────────────────────────────────────────
    app.add_handler(CommandHandler("whitelist", cmd_whitelist))
    app.add_handler(CommandHandler("users", cmd_users))
    app.add_handler(CommandHandler("broadcast", cmd_broadcast))

    # ── Direct translation (private chat text) ─────────────
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE, handle_direct_text))

    logger.info("Bot starting (polling)")
    app.run_polling(
        drop_pending_updates=True,
        allowed_updates=["message", "callback_query", "my_chat_member"],
    )


if __name__ == "__main__":
    main()
