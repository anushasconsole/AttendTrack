"""
app/services/whatsapp.py — Twilio WhatsApp alerts.
Degrades gracefully to log-only if Twilio is not configured.
"""
from app.core.config import (
    TWILIO_SID, TWILIO_AUTH_TOKEN, TWILIO_FROM, WHATSAPP_DEFAULT_CC,
)

WHATSAPP_READY = False
_twilio_client = None

try:
    if TWILIO_SID and TWILIO_AUTH_TOKEN and TWILIO_FROM:
        from twilio.rest import Client as TwilioClient
        _twilio_client = TwilioClient(TWILIO_SID, TWILIO_AUTH_TOKEN)
        WHATSAPP_READY = True
        print("✅ Twilio WhatsApp ready — overtime alerts will be sent")
    else:
        print("⚠️  Twilio env vars not set — overtime alerts will be LOGGED ONLY")
except ImportError:
    print("⚠️  twilio package not installed — overtime alerts will be LOGGED ONLY")


def send_whatsapp_alert(to_phone: str, message: str) -> dict:
    """
    Send a WhatsApp message via Twilio.
    Normalises phone numbers from any format to whatsapp:+{digits}.
    Falls back to logging if Twilio not configured.
    """
    if not to_phone:
        return {"sent": False, "reason": "no_phone_on_file"}
    if not WHATSAPP_READY:
        print(f"[WHATSAPP NOT SENT — no Twilio config] to={to_phone}: {message[:80]}…", flush=True)
        return {"sent": False, "reason": "twilio_not_configured"}
    try:
        raw = str(to_phone).strip()
        if raw.lower().startswith("whatsapp:"):
            raw = raw[9:]
        digits = raw.lstrip("+").strip()
        if len(digits) <= 10:
            digits = WHATSAPP_DEFAULT_CC.lstrip("+") + digits
        to_wa = f"whatsapp:+{digits}"
        msg = _twilio_client.messages.create(from_=TWILIO_FROM, to=to_wa, body=message)
        print(f"[WHATSAPP SENT] sid={msg.sid} to={to_wa}", flush=True)
        return {"sent": True, "sid": msg.sid, "to": to_wa}
    except Exception as e:
        print(f"[WHATSAPP FAILED] to={to_phone}: {type(e).__name__}: {e}", flush=True)
        return {"sent": False, "reason": str(e)}
