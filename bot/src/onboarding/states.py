"""Onboarding FSM states.

IDLE → QR_PENDING → WA_CONNECTED → LINKING → DONE
"""

IDLE = "idle"
QR_PENDING = "qr_pending"
WA_CONNECTED = "wa_connected"
LINKING = "linking"
DONE = "done"

# Conversation handler states for python-telegram-bot
STEP_LANGUAGE = 1
