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
from .handlers.translate import handle_direct_media, handle_direct_text
from .handlers.chats import cb_chat_action, cb_link_chat, cmd_add, cmd_chats
from .handlers.groups import cb_cmd_add, handle_group_message, handle_my_chat_member
from .onboarding.wizard import cmd_start
from .redis_sub import redis_subscriber_loop, set_bot_app, set_event_loop

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)
# httpx logs every request at INFO, and every Telegram call carries the bot token in its
# URL — which put a working token into docker logs, log shippers and any backup of them.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)



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

    # concurrent_updates(True): process updates concurrently. Without it a single slow
    # handler (media analysis/translate up to ~2 min) blocks EVERY user's updates serially.
    app = Application.builder().token(token).post_init(post_init).concurrent_updates(True).build()

    # Inject bot reference into redis_sub module
    set_bot_app(app)

    # ── Onboarding ────────────────────────────────────────
    # /start is the only entry: it hands over the Mini App, which does the rest over HTTP.
    app.add_handler(CommandHandler("start", cmd_start))

    # ── Chat management ───────────────────────────────────
    app.add_handler(CommandHandler("chats", cmd_chats))
    app.add_handler(CommandHandler("add", cmd_add))
    app.add_handler(CallbackQueryHandler(cb_link_chat, pattern=r"^link:"))
    app.add_handler(CallbackQueryHandler(cb_chat_action, pattern=r"^chat:(pause|resume|settings|lang|summary|close):"))

    # ── Group tracking ────────────────────────────────────
    app.add_handler(ChatMemberHandler(handle_my_chat_member, ChatMemberHandler.MY_CHAT_MEMBER))
    app.add_handler(CallbackQueryHandler(cb_cmd_add, pattern="^cmd:add$"))
    # Ordinary group traffic refreshes membership. Without it a group the bot joined
    # before this existed would never reach the picker: there is no event to replay and
    # no API to list the bot's own chats. group=1 and block=False keep it out of the way
    # of the real handlers; sync_group itself is throttled to once an hour per chat.
    app.add_handler(
        MessageHandler(filters.ChatType.GROUPS, handle_group_message, block=False),
        group=1,
    )

    # ── Media analysis ─────────────────────────────────────
    app.add_handler(CallbackQueryHandler(cb_analyze_media, pattern=r"^analyze:\d+$"))
    app.add_handler(CallbackQueryHandler(cb_noop, pattern=r"^noop$"))

    # ── Admin ─────────────────────────────────────────────
    app.add_handler(CommandHandler("whitelist", cmd_whitelist))
    app.add_handler(CommandHandler("users", cmd_users))
    app.add_handler(CommandHandler("broadcast", cmd_broadcast))

    # ── Direct translation (private chat text) ─────────────
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE, handle_direct_text))

    # ── Direct media analysis (private chat media) ────────
    app.add_handler(MessageHandler(
        (filters.PHOTO | filters.Document.ALL | filters.AUDIO | filters.VOICE | filters.VIDEO_NOTE) & filters.ChatType.PRIVATE,
        handle_direct_media,
    ))

    logger.info("Bot starting (polling)")
    app.run_polling(
        # Keep what arrived while the bot was restarting. Dropping it meant a /add or an
        # Analyze tap sent during a deploy vanished with no feedback to the user.
        drop_pending_updates=False,
        allowed_updates=["message", "callback_query", "my_chat_member"],
    )


if __name__ == "__main__":
    main()
