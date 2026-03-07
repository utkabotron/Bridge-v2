"""User-facing message templates for Bridge v2 bot."""

TEMPLATES = {
    # ── /start (unauthorized) ─────────────────────────────
    "not_authorized": (
        "*Access restricted.*\n\n"
        "This bot is invite-only. Contact the administrator to get access."
    ),

    # ── /start (universal) ─────────────────────────────────
    "welcome_start": (
        "👋 *Bridge — WhatsApp → Telegram*\n\n"
        "I forward WhatsApp messages to Telegram groups with automatic translation.\n\n"
        "Open the app below to connect WhatsApp and manage your chat pairs."
    ),

    # ── /start (authorized, first time) ──────────────────
    "welcome_new": (
        "👋 *Welcome!*\n\n"
        "I forward WhatsApp messages to Telegram with automatic translation.\n\n"
        "Press the button below to connect your WhatsApp account."
    ),

    # ── /start (authorized, returning) ───────────────────
    "welcome_back": (
        "*Hi again!*\n\n"
        "Use the buttons below to manage your setup."
    ),

    # ── Onboarding steps ──────────────────────────────────
    "onboarding_step1": (
        "🔵⚪⚪⚪⚪\n\n"
        "*Step 1 — Connect WhatsApp*\n\n"
        "I'll generate a QR code. Open WhatsApp on your phone:\n"
        "*Settings → Linked Devices → Link a Device*\n\n"
        "Press the button to start."
    ),

    "onboarding_step2_wait": (
        "🔵🔵⚪⚪⚪\n\n"
        "*Step 2 — Scan QR Code*\n\n"
        "Open the link below in a browser and scan the QR code with WhatsApp:\n\n"
        "{qr_url}\n\n"
        "_The link is valid for 60 seconds. Waiting for you to scan..._"
    ),

    "onboarding_step3": (
        "🔵🔵🔵⚪⚪\n\n"
        "*Step 3 — Create a Telegram group*\n\n"
        "1. Create a new Telegram group (any name)\n"
        "2. Add me (@{bot_username}) to the group\n"
        "3. Press the button below once done"
    ),

    "onboarding_step4": (
        "🔵🔵🔵🔵⚪\n\n"
        "*Step 4 — Select WhatsApp chat*\n\n"
        "Choose which WhatsApp group to forward to *{tg_group}*:"
    ),

    "onboarding_complete": (
        "🔵🔵🔵🔵🔵\n\n"
        "✅ *All set!*\n\n"
        "Messages from *{wa_name}* will now arrive here with translation.\n\n"
        "Use /chats to manage, /add to add more chats."
    ),

    "onboarding_webapp_linked": (
        "✅ *WhatsApp connected!*\n\n"
        "Selected group: *{wa_name}*\n\n"
        "Now:\n"
        "1. Create a new Telegram group\n"
        "2. Add @{bot_username} to the group\n"
        "3. Type /done in that group"
    ),

    "onboarding_done_success": (
        "✅ *All set!*\n\n"
        "Linked *{wa_name}* → *{tg_title}*\n\n"
        "Messages from this WhatsApp group will now arrive here with translation.\n\n"
        "Use /chats to manage, /add to add more chats."
    ),

    "onboarding_done_no_pending": (
        "No pending WhatsApp connection.\n\n"
        "Use /start in a private chat with me first to connect WhatsApp."
    ),

    "onboarding_done_group_only": (
        "This command only works in a Telegram group.\n\n"
        "Add me to a group first, then type /done there."
    ),

    "onboarding_wa_connected": (
        "🔵🔵🔵⚪⚪\n\n"
        "✅ *WhatsApp connected!*\n\n"
        "Now create a Telegram group and add me to it.\n\n"
        "1. Tap ➕ → *New Group* in Telegram\n"
        "2. Add @{bot_username}\n"
        "3. Press *Done* below"
    ),

    # ── /chats ────────────────────────────────────────────
    "chats_empty": (
        "No linked chats.\n\n"
        "Add me to a Telegram group and use /add there."
    ),
    "chats_header": "*Your linked chats:*\n\n",
    "chat_item": "{idx}. {wa_name} → {tg_title} [{status}]\n",

    # ── /add ──────────────────────────────────────────────
    "add_group_only": "This command only works in a Telegram group. Add me to a group first.",
    "add_not_connected": "WhatsApp is not connected. Use /start to set up.",
    "add_select_header": "*Select a WhatsApp chat* to link to *{tg_group}*:",
    "add_success": "✅ Linked *{wa_name}* → *{tg_title}*",
    "add_already_linked": "This chat is already linked.",

    # ── /pause / /resume ──────────────────────────────────
    "chat_paused": "⏸ Chat paused.",
    "chat_resumed": "▶️ Chat resumed.",

    # ── Admin ─────────────────────────────────────────────
    "admin_whitelist_added": "✅ @{username} added to whitelist.",
    "admin_whitelist_removed": "❌ @{username} removed from whitelist.",
    "admin_users_header": "*All users:*\n\n",
    "admin_user_item": "• {tg_user_id} @{username} — {status}\n",

    # ── Bot added to group ─────────────────────────────────
    "bot_added_as_admin": (
        "✅ *Ready!*\n\n"
        "Use /add to link a WhatsApp group to this chat."
    ),

    # ── Errors ────────────────────────────────────────────
    "error_generic": "Something went wrong. Please try again.",
    "error_wa_service": "WhatsApp service is unavailable. Contact admin.",
    "error_no_wa_groups": "No WhatsApp groups found. Make sure you have groups in WhatsApp.",
}


def render(key: str, escape: bool = False, **kwargs) -> str:
    tmpl = TEMPLATES.get(key)
    if tmpl is None:
        raise KeyError(f"Template '{key}' not found")
    if kwargs:
        if escape:
            from ..utils.telegram_format import escape_md
            kwargs = {k: escape_md(str(v)) for k, v in kwargs.items()}
        return tmpl.format(**kwargs)
    return tmpl
