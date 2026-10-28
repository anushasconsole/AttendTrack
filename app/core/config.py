"""
app/core/config.py — All environment variables and shared constants.
Every other module imports from here instead of calling os.getenv() directly.
"""
import os
from dotenv import load_dotenv

load_dotenv()

# ── Database ─────────────────────────────────────────────────────────────────
DATABASE_URL: str = os.getenv("DATABASE_URL", "")
if not DATABASE_URL:
    raise RuntimeError("❌ DATABASE_URL not set in .env file!")

# ── Overtime ──────────────────────────────────────────────────────────────────
OVERTIME_LIMIT_HOURS: float = float(os.getenv("OVERTIME_LIMIT_HOURS", "105"))
OVERTIME_WARN_AT: float     = float(os.getenv("OVERTIME_WARN_AT", "0.9"))

# ── Twilio / WhatsApp ─────────────────────────────────────────────────────────
TWILIO_SID: str          = os.getenv("TWILIO_ACCOUNT_SID", "")
TWILIO_AUTH_TOKEN: str   = os.getenv("TWILIO_AUTH_TOKEN", "")
TWILIO_FROM: str         = os.getenv("TWILIO_WHATSAPP_FROM", "")
WHATSAPP_DEFAULT_CC: str = os.getenv("WHATSAPP_DEFAULT_CC", "+91")

# ── AI (Groq / OpenRouter) ────────────────────────────────────────────────────
AI_PROVIDER: str = os.getenv("AI_PROVIDER", "groq").lower()
AI_API_KEY: str  = os.getenv("AI_API_KEY", "")

AI_PROVIDER_CONFIG: dict = {
    "groq": {
        "base_url": "https://api.groq.com/openai/v1/chat/completions",
        "default_model": "llama-3.3-70b-versatile",
    },
    "openrouter": {
        "base_url": "https://openrouter.ai/api/v1/chat/completions",
        "default_model": "meta-llama/llama-3.3-70b-instruct:free",
    },
}
AI_MODEL: str  = os.getenv(
    "AI_MODEL",
    AI_PROVIDER_CONFIG.get(AI_PROVIDER, AI_PROVIDER_CONFIG["groq"])["default_model"],
)
AI_READY: bool = bool(AI_API_KEY) and AI_PROVIDER in AI_PROVIDER_CONFIG

# ── Redis ─────────────────────────────────────────────────────────────────────
REDIS_URL: str = os.getenv("REDIS_URL", "")

# ── JWT ───────────────────────────────────────────────────────────────────────
JWT_SECRET_KEY: str   = os.getenv("JWT_SECRET_KEY", "CHANGE_ME_in_production_use_random_32chars")
JWT_EXPIRE_HOURS: int = int(os.getenv("JWT_EXPIRE_HOURS", "24"))

# ── Frontend directory ────────────────────────────────────────────────────────
import pathlib
BASE_DIR: pathlib.Path    = pathlib.Path(__file__).resolve().parent.parent.parent
FRONTEND_DIR: pathlib.Path = BASE_DIR / "frontend"
