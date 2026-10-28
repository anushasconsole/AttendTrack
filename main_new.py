"""
main_new.py — AttendTrack API entry point (modular structure).

Thin orchestrator: lifespan, middleware, router mounts, static files.
All business logic lives in app/routers/ and app/services/.

To run:
    uvicorn main_new:app --host 0.0.0.0 --port 8000
"""
import asyncio
import json
import os
from contextlib import asynccontextmanager
from dotenv import load_dotenv

load_dotenv()

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.core.config import FRONTEND_DIR
from app.core.database import init_db
from redis_layer import get_redis, close_redis, ws_register, ws_unregister, subscribe_loop

# ── Import all routers ────────────────────────────────────────────────────────
from app.routers.auth_routes  import router as auth_router
from app.routers.attendance   import router as attendance_router
from app.routers.manager      import router as manager_router
from app.routers.hr           import router as hr_router
from app.routers.analytics    import router as analytics_router
from app.routers.ml           import router as ml_router
from app.routers.reports      import router as reports_router, boot_schedules, set_scheduler, SCHEDULER_READY
from app.routers.employee_qa  import router as qa_router

# ── Optional APScheduler ──────────────────────────────────────────────────────
_scheduler   = None
_pubsub_task = None


@asynccontextmanager
async def lifespan(application: FastAPI):
    global _scheduler, _pubsub_task

    # Initialise DB (creates tables + seeds if empty)
    init_db()

    # Redis pub/sub subscriber — one per worker
    _pubsub_task = asyncio.create_task(subscribe_loop())

    if SCHEDULER_READY:
        from apscheduler.schedulers.asyncio import AsyncIOScheduler
        _scheduler = AsyncIOScheduler()
        set_scheduler(_scheduler)
        await boot_schedules()
        _scheduler.start()
        print("✅ Scheduler started")

    yield

    # Shutdown
    if _pubsub_task and not _pubsub_task.done():
        _pubsub_task.cancel()
    await close_redis()
    if _scheduler and _scheduler.running:
        _scheduler.shutdown(wait=False)
        print("Scheduler stopped")


# ── App ───────────────────────────────────────────────────────────────────────
app = FastAPI(title="AttendTrack API", version="5.0.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Serve the frontend static directory
_frontend_str = str(FRONTEND_DIR)
if os.path.isdir(_frontend_str):
    app.mount("/frontend", StaticFiles(directory=_frontend_str), name="frontend")

# ── Mount all routers ─────────────────────────────────────────────────────────
for _r in [auth_router, attendance_router, manager_router, hr_router,
           analytics_router, ml_router, reports_router, qa_router]:
    app.include_router(_r)


# ── Static portal routes ──────────────────────────────────────────────────────

@app.get("/")
async def root():
    return {
        "msg":     "AttendTrack API v5",
        "portals": {
            "labour":  "/labour",
            "manager": "/manager",
            "hr":      "/hr",
        },
    }


@app.get("/labour")
async def labour_portal():
    p = FRONTEND_DIR / "labour.html"
    return FileResponse(str(p)) if p.exists() else {"error": "labour.html not found"}


@app.get("/manager")
async def manager_portal():
    p = FRONTEND_DIR / "manager.html"
    return FileResponse(str(p)) if p.exists() else {"error": "manager.html not found"}


@app.get("/hr")
async def hr_portal():
    p = FRONTEND_DIR / "hr.html"
    return FileResponse(str(p)) if p.exists() else {"error": "hr.html not found"}


@app.get("/portal.js")
async def portal_js():
    p = FRONTEND_DIR / "portal.js"
    return FileResponse(str(p)) if p.exists() else {"error": "portal.js not found"}

@app.get("/frontend/portal.js")
async def legacy_portal_js():
    return await portal_js()


# ── Health ─────────────────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    from app.core.config import AI_READY
    from app.services.face import FACE_READY
    from app.routers.ml import ML_ENGINE_READY
    r = await get_redis()
    redis_ok = False
    if r:
        try:
            await r.ping()
            redis_ok = True
        except Exception:
            pass
    from redis_layer import REDIS_READY
    return {
        "status":    "ok",
        "version":   "5.0.0",
        "auth":      "jwt+bcrypt",
        "face_ready": FACE_READY,
        "face_bypass": not FACE_READY,
        "db":        "postgresql",
        "redis":     "connected" if redis_ok else ("not_configured" if not REDIS_READY else "unreachable"),
        "ml_engine": ML_ENGINE_READY,
        "ai_ready":  AI_READY,
        "scheduler": SCHEDULER_READY,
    }


# ── WebSocket ─────────────────────────────────────────────────────────────────

@app.websocket("/ws/{user_id}")
async def websocket_endpoint(websocket: WebSocket, user_id: str):
    """
    WebSocket per user/channel.  Client must send {"auth": "<bearer_token>"}
    as the first message after connecting.
    """
    await websocket.accept()
    try:
        auth_msg  = await asyncio.wait_for(websocket.receive_text(), timeout=10.0)
        auth_data = json.loads(auth_msg)
        token     = auth_data.get("auth", "")
        if not token:
            await websocket.send_text(json.dumps({"type": "error", "message": "auth token required"}))
            await websocket.close(code=4001)
            return
        from auth import _decode_token, _is_blacklisted
        payload = _decode_token(token)
        jti = payload.get("jti", "")
        if jti and await _is_blacklisted(jti):
            await websocket.send_text(json.dumps({"type": "error", "message": "token revoked"}))
            await websocket.close(code=4001)
            return
        await websocket.send_text(json.dumps({"type": "connected", "user_id": user_id}))
    except asyncio.TimeoutError:
        await websocket.close(code=4002); return
    except Exception:
        await websocket.close(code=4003); return

    await ws_register(user_id, websocket)
    try:
        while True:
            data = await websocket.receive_text()
            try:
                msg = json.loads(data)
                if msg.get("type") == "ping":
                    await websocket.send_text(json.dumps({"type": "pong"}))
            except Exception:
                pass
    except WebSocketDisconnect:
        pass
    finally:
        await ws_unregister(user_id, websocket)
