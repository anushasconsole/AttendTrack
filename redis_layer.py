"""
redis_layer.py — AttendTrack Redis integration
================================================
Provides three production features:

1. DISTRIBUTED LOCKING  (RedisLock)
   Prevents duplicate checkins/checkouts when multiple API workers or
   concurrent requests race on the same employee_id.
   Uses SET NX EX (atomic) — no Lua script needed.

2. RATE LIMITING  (RateLimiter)
   Token-bucket rate limiter backed by Redis sorted sets.
   Used on login (per IP), checkin/checkout (per employee),
   face verify (per employee), and AI Q&A (per employee).

3. WebSocket CONNECTION REGISTRY  (WSRegistry)
   Tracks which WebSocket connections belong to which user/role across
   all worker processes.  A worker that updates an attendance record
   publishes the event to a Redis Pub/Sub channel; every worker's
   subscriber task then pushes it to its own locally-connected sockets.

All three degrade gracefully if Redis is not available — the app works
without Redis but loses distributed guarantees (fine for single-instance
deploys like a free Railway tier).
"""

import asyncio
import json
import time
import os
from contextlib import asynccontextmanager
from typing import Optional

REDIS_URL = os.getenv("REDIS_URL", "")  # e.g. redis://localhost:6379/0

# ── Try to import redis.asyncio (redis-py ≥ 4.2) ────────────────────────────
REDIS_READY = False
_redis: Optional[object] = None   # will hold the redis.asyncio.Redis client

try:
    import redis.asyncio as aioredis
    REDIS_READY = bool(REDIS_URL)
    if not REDIS_READY:
        print("⚠️  REDIS_URL not set — Redis features disabled (locking/rate-limit/WS pub-sub will be no-ops)")
    else:
        print(f"✅ Redis configured — url={REDIS_URL[:30]}…")
except ImportError:
    print("⚠️  redis package not installed — Redis features disabled")


async def get_redis():
    """Return (and lazily create) the shared async Redis client."""
    global _redis
    if not REDIS_READY:
        return None
    if _redis is None:
        _redis = aioredis.from_url(
            REDIS_URL,
            encoding="utf-8",
            decode_responses=True,
            socket_connect_timeout=3,
            socket_timeout=3,
            retry_on_timeout=True,
            max_connections=20,
        )
    return _redis


async def close_redis():
    global _redis
    if _redis:
        await _redis.aclose()
        _redis = None


# ─────────────────────────────────────────────────────────────────────────────
# 1. DISTRIBUTED LOCK
# ─────────────────────────────────────────────────────────────────────────────

class RedisLock:
    """
    Async context manager that acquires a Redis SET NX EX lock.

    Usage:
        async with RedisLock(redis, "checkin:EMP001", ttl=10):
            # only one worker executes this block at a time
            ...

    If Redis is unavailable, the lock is a no-op (falls through immediately).
    Raises LockNotAcquired if another worker holds the lock.
    """

    class LockNotAcquired(Exception):
        pass

    def __init__(self, redis_client, key: str, ttl: int = 10):
        self._r     = redis_client
        self._key   = f"lock:{key}"
        self._ttl   = ttl
        self._token = str(time.monotonic_ns())   # unique per acquisition attempt

    async def __aenter__(self):
        if self._r is None:
            return self   # no-op
        acquired = await self._r.set(self._key, self._token, nx=True, ex=self._ttl)
        if not acquired:
            raise RedisLock.LockNotAcquired(f"Operation already in progress for {self._key}")
        return self

    async def __aexit__(self, exc_type, exc, tb):
        if self._r is None:
            return
        # Only delete if we still own it (guards against TTL expiry + re-acquisition)
        current = await self._r.get(self._key)
        if current == self._token:
            await self._r.delete(self._key)


# ─────────────────────────────────────────────────────────────────────────────
# 2. RATE LIMITER  (sliding-window counter via Redis INCR + EXPIRE)
# ─────────────────────────────────────────────────────────────────────────────

class RateLimiter:
    """
    Simple fixed-window rate limiter.

    Limits:
      login         : 10 attempts / 60s  per IP
      checkin       :  3 attempts / 60s  per employee_id
      checkout      :  3 attempts / 60s  per employee_id
      face_verify   :  5 attempts / 60s  per employee_id
      ai_ask        : 10 attempts / 60s  per employee_id

    If Redis is unavailable, all checks pass (no-op).
    Raises RateLimitExceeded when the limit is hit.
    """

    class RateLimitExceeded(Exception):
        def __init__(self, action: str, retry_after: int):
            super().__init__(f"Rate limit exceeded for {action}. Retry after {retry_after}s.")
            self.retry_after = retry_after

    # (max_requests, window_seconds)
    LIMITS: dict = {
        "login":       (10,  60),
        "checkin":     ( 3,  60),
        "checkout":    ( 3,  60),
        "face_verify": ( 5,  60),
        "face_register": (3, 300),
        "ai_ask":      (10,  60),
    }

    def __init__(self, redis_client):
        self._r = redis_client

    async def check(self, action: str, identifier: str):
        """
        Increment the counter for (action, identifier) and raise if over limit.
        identifier is typically an IP address or employee_id.
        """
        if self._r is None:
            return   # Redis not available — skip rate limiting

        max_req, window = self.LIMITS.get(action, (30, 60))
        key = f"rl:{action}:{identifier}"

        pipe = self._r.pipeline()
        pipe.incr(key)
        pipe.ttl(key)
        count, ttl = await pipe.execute()

        # Set expiry on the first hit
        if ttl == -1:
            await self._r.expire(key, window)
            ttl = window

        if count > max_req:
            raise RateLimiter.RateLimitExceeded(action, ttl)


# ─────────────────────────────────────────────────────────────────────────────
# 3. WebSocket CONNECTION REGISTRY + PUB/SUB
# ─────────────────────────────────────────────────────────────────────────────

PUBSUB_CHANNEL = "attendtrack:events"

# Local (per-process) registry: maps user_id → set of WebSocket objects
# This is intentionally in-process; cross-process delivery is handled by
# Redis Pub/Sub (see subscribe_loop below).
import collections
_local_ws_registry: dict = collections.defaultdict(set)
_registry_lock = asyncio.Lock()


async def ws_register(user_id: str, ws):
    async with _registry_lock:
        _local_ws_registry[user_id].add(ws)


async def ws_unregister(user_id: str, ws):
    async with _registry_lock:
        _local_ws_registry[user_id].discard(ws)
        if not _local_ws_registry[user_id]:
            del _local_ws_registry[user_id]


async def ws_send_to_user(user_id: str, payload: dict):
    """Send a JSON payload to all locally-connected sockets for user_id."""
    sockets = set(_local_ws_registry.get(user_id, set()))
    dead = set()
    msg = json.dumps(payload)
    for ws in sockets:
        try:
            await ws.send_text(msg)
        except Exception:
            dead.add(ws)
    if dead:
        async with _registry_lock:
            _local_ws_registry[user_id] -= dead


async def publish_event(event: dict):
    """
    Publish an attendance event to Redis Pub/Sub so every worker can
    forward it to its locally-connected WebSocket clients.

    event shape:
      { "type": "attendance_update",
        "attendance_id": 42,
        "employee_id": "LAB001",
        "status": "pending_hr",
        "updated_by_role": "manager",
        "timestamp": "2026-07-01T09:30:00" }
    """
    r = await get_redis()
    if r is None:
        # No Redis — fall back to local-only delivery
        await _deliver_event(event)
        return
    try:
        await r.publish(PUBSUB_CHANNEL, json.dumps(event))
    except Exception as e:
        print(f"[WS PUBSUB] publish failed: {e}", flush=True)
        await _deliver_event(event)


async def _deliver_event(event: dict):
    """Deliver event to locally-connected sockets for the employee."""
    employee_id = event.get("employee_id")
    if employee_id:
        await ws_send_to_user(employee_id, event)
    # Also fan out to any manager/HR sockets subscribed to role-level topics
    role_topic = event.get("site_id")
    if role_topic:
        await ws_send_to_user(f"site:{role_topic}", event)
    await ws_send_to_user("hr:broadcast", event)


async def subscribe_loop():
    """
    Long-running coroutine (one per worker process) that subscribes to
    the Redis Pub/Sub channel and fans events out to local WebSocket clients.
    Started in the FastAPI lifespan.
    """
    r = await get_redis()
    if r is None:
        print("[WS PUBSUB] Redis not available — skipping subscribe loop", flush=True)
        return

    pubsub = r.pubsub()
    await pubsub.subscribe(PUBSUB_CHANNEL)
    print(f"✅ WebSocket pub/sub subscriber active on channel '{PUBSUB_CHANNEL}'", flush=True)

    try:
        async for raw in pubsub.listen():
            if raw["type"] != "message":
                continue
            try:
                event = json.loads(raw["data"])
                await _deliver_event(event)
            except Exception as e:
                print(f"[WS PUBSUB] deliver error: {e}", flush=True)
    except asyncio.CancelledError:
        pass
    except Exception as e:
        print(f"[WS PUBSUB] subscribe_loop crashed: {e}", flush=True)
    finally:
        await pubsub.unsubscribe(PUBSUB_CHANNEL)
        print("[WS PUBSUB] subscriber stopped", flush=True)
