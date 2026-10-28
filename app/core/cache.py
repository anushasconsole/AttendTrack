"""
app/core/cache.py — Redis response cache helpers.

Key patterns:
  cache:hr:summary              TTL 30s
  cache:mgr:stats:{manager_id}  TTL 30s
  cache:analytics:emp:{id}      TTL 60s
  cache:analytics:site:{id}     TTL 60s
  cache:employees               TTL 120s

All functions degrade silently if Redis is unavailable.
"""
import json
from redis_layer import get_redis


async def cache_get(key: str):
    """Return parsed JSON from cache, or None on miss / Redis unavailable."""
    r = await get_redis()
    if not r:
        return None
    try:
        raw = await r.get(key)
        return json.loads(raw) if raw else None
    except Exception:
        return None


async def cache_set(key: str, value, ttl: int):
    """Store value as JSON in Redis with TTL seconds. Silent on failure."""
    r = await get_redis()
    if not r:
        return
    try:
        await r.setex(key, ttl, json.dumps(value, default=str))
    except Exception:
        pass


async def cache_invalidate(*keys: str):
    """Delete one or more cache keys on writes. Silent on failure."""
    r = await get_redis()
    if not r:
        return
    try:
        await r.delete(*keys)
    except Exception:
        pass
