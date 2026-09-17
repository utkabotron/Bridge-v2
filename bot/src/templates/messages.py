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



    # ── Pair created ──────────────────────────────────────






    "onboarding_done_success": (
        "✅ *All set!*\n\n"
        "Linked *{wa_name}* → *{tg_title}*\n\n"
        "Messages from this WhatsApp group will now arrive here with translation.\n\n"
        "Use /chats to manage, /add to add more chats."
    ),




    "wa_connected_notice": (
        "✅ *WhatsApp connected.*\n\n"
        "Open the app to link a chat."
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

    # ── /pause / /resume ──────────────────────────────────
    "chat_paused": "⏸ Chat paused.",
    "chat_settings_closed": "Settings closed. Open /chats to return.",
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
