"""
app/services/llm.py — LLM call wrapper (Groq / OpenRouter, OpenAI-compatible).
Never raises — always returns {"ok": bool, "text": str|None, "error": str|None}.
"""
import httpx
from app.core.config import AI_READY, AI_PROVIDER, AI_API_KEY, AI_MODEL, AI_PROVIDER_CONFIG

if AI_READY:
    print(f"✅ AI analysis ready — provider={AI_PROVIDER} model={AI_MODEL}")
else:
    print("⚠️  AI_API_KEY not set — AI endpoints will return a clear error message")


async def call_llm(system_prompt: str, user_prompt: str, max_tokens: int = 600) -> dict:
    if not AI_READY:
        return {"ok": False, "text": None,
                "error": "AI not configured — set AI_API_KEY in .env"}
    cfg     = AI_PROVIDER_CONFIG[AI_PROVIDER]
    headers = {"Authorization": f"Bearer {AI_API_KEY}", "Content-Type": "application/json"}
    if AI_PROVIDER == "openrouter":
        headers["HTTP-Referer"] = "https://attendtrack.app"
        headers["X-Title"]      = "AttendTrack"
    payload = {
        "model":    AI_MODEL,
        "messages": [{"role": "system", "content": system_prompt},
                     {"role": "user",   "content": user_prompt}],
        "max_tokens":  max_tokens,
        "temperature": 0.3,
    }
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(cfg["base_url"], headers=headers, json=payload)
        if resp.status_code == 401:
            return {"ok": False, "text": None, "error": "AI API key rejected (401)"}
        if resp.status_code in (400, 404):
            return {"ok": False, "text": None,
                    "error": f"AI model '{AI_MODEL}' rejected ({resp.status_code}): {resp.text[:200]}"}
        if resp.status_code == 429:
            return {"ok": False, "text": None, "error": "AI rate limit (429) — try again shortly"}
        if resp.status_code != 200:
            return {"ok": False, "text": None,
                    "error": f"AI error {resp.status_code}: {resp.text[:300]}"}
        text = resp.json()["choices"][0]["message"]["content"]
        return {"ok": True, "text": text, "error": None}
    except Exception as e:
        return {"ok": False, "text": None, "error": f"{type(e).__name__}: {e}"}
