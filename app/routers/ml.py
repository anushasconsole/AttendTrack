"""
app/routers/ml.py — ML training, prediction, anomaly detection, behaviour.
"""
import asyncio
import io
from fastapi import APIRouter, HTTPException, Depends

from auth import require_hr, require_manager_or_hr, TokenData
from app.core.database import get_db, release_db
from app.core.config import OVERTIME_LIMIT_HOURS
from redis_layer import get_redis

router = APIRouter()

# ── Optional ML engine ────────────────────────────────────────────────────────
ML_ENGINE_READY = False
ml_engine = None
try:
    import ml_engine as _ml
    _ml.init(OVERTIME_LIMIT_HOURS)
    ml_engine = _ml
    ML_ENGINE_READY = True
    print(f"✅ ml_engine loaded — overtime limit={OVERTIME_LIMIT_HOURS}h")
except Exception as _e:
    print(f"⚠️  ml_engine not loaded: {_e}")


# ── Model persistence helpers ─────────────────────────────────────────────────

async def ml_save_model(employee_id: str):
    if not ML_ENGINE_READY:
        return
    r = await get_redis()
    if not r:
        return
    try:
        import joblib
        model_dict = ml_engine._models.get(employee_id)
        if not model_dict:
            return
        buf = io.BytesIO()
        await asyncio.to_thread(joblib.dump, model_dict, buf)
        await r.setex(f"ml:model:{employee_id}", 90000, buf.getvalue())
        print(f"[ML] Persisted model for {employee_id} to Redis", flush=True)
    except Exception as e:
        print(f"[ML] Failed to persist model for {employee_id}: {e}", flush=True)


async def ml_load_model(employee_id: str) -> bool:
    if not ML_ENGINE_READY:
        return False
    try:
        import joblib, os
        import redis.asyncio as aioredis
        redis_url = os.getenv("REDIS_URL", "")
        if not redis_url:
            return False
        raw_r = aioredis.from_url(redis_url, decode_responses=False, socket_timeout=3)
        blob = await raw_r.get(f"ml:model:{employee_id}")
        await raw_r.aclose()
        if not blob:
            return False
        model_dict = await asyncio.to_thread(joblib.load, io.BytesIO(blob))
        ml_engine._models[employee_id] = model_dict
        return True
    except Exception as e:
        print(f"[ML] Failed to restore model for {employee_id}: {e}", flush=True)
        return False


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.post("/api/ml/train/{employee_id}")
async def ml_train_employee(employee_id: str,
                            current: TokenData = Depends(require_hr)):
    if not ML_ENGINE_READY:
        raise HTTPException(503, "ml_engine not loaded")
    conn = get_db()
    try:
        c = conn.cursor()
        c.execute("SELECT employee_id FROM users WHERE employee_id=%s AND role='labour'",
                  (employee_id.upper(),))
        if not c.fetchone():
            raise HTTPException(404, "Labour employee not found")
        c.execute("""SELECT date,login_time,logout_time,hours_worked,status
                     FROM attendance WHERE employee_id=%s ORDER BY date ASC""",
                  (employee_id.upper(),))
        records = [dict(r) for r in c.fetchall()]
    finally:
        release_db(conn)

    result = await asyncio.to_thread(ml_engine.train_employee, employee_id.upper(), records)
    if result.get("ok"):
        await ml_save_model(employee_id.upper())
    return result


@router.post("/api/ml/train-all")
async def ml_train_all(current: TokenData = Depends(require_hr)):
    if not ML_ENGINE_READY:
        raise HTTPException(503, "ml_engine not loaded")
    conn = get_db()
    try:
        c = conn.cursor()
        c.execute("SELECT employee_id FROM users WHERE role='labour'")
        employees = [r["employee_id"] for r in c.fetchall()]
        emp_records: dict = {eid: [] for eid in employees}
        if employees:
            c.execute("""SELECT employee_id,date,login_time,logout_time,hours_worked,status
                         FROM attendance WHERE employee_id=ANY(%s) ORDER BY employee_id,date ASC""",
                      (employees,))
            for row in c.fetchall():
                emp_records[row["employee_id"]].append(dict(row))
    finally:
        release_db(conn)

    result = await asyncio.to_thread(ml_engine.retrain_all, emp_records)
    ok_ids = [eid for eid, r in result.get("details", {}).items() if r.get("ok")]
    for eid in ok_ids:
        await ml_save_model(eid)
    print(f"[ML] Persisted {len(ok_ids)} models to Redis", flush=True)
    return result


@router.get("/api/ml/predict/{employee_id}")
async def ml_predict_overtime(employee_id: str,
                              current: TokenData = Depends(require_manager_or_hr)):
    if not ML_ENGINE_READY:
        raise HTTPException(503, "ml_engine not loaded")
    if employee_id.upper() not in ml_engine._models:
        await ml_load_model(employee_id.upper())

    from datetime import datetime
    from app.services.overtime import _month_bounds
    conn = get_db()
    try:
        c = conn.cursor()
        c.execute("SELECT name FROM users WHERE employee_id=%s", (employee_id.upper(),))
        user = c.fetchone()
        if not user:
            raise HTTPException(404, "Employee not found")
        as_of = datetime.now()
        ps, pe = _month_bounds(as_of)
        c.execute("""SELECT date,hours_worked FROM attendance
                     WHERE employee_id=%s AND date>=%s AND date<%s
                       AND hours_worked IS NOT NULL ORDER BY date ASC""",
                  (employee_id.upper(), ps, pe))
        month_records = [dict(r) for r in c.fetchall()]
    finally:
        release_db(conn)

    result = await asyncio.to_thread(
        ml_engine.predict_overtime, employee_id.upper(), month_records, as_of)
    result["name"] = user["name"]
    return result


@router.get("/api/ml/anomalies/{employee_id}")
async def ml_anomalies(employee_id: str, lookback_days: int = 60,
                       current: TokenData = Depends(require_manager_or_hr)):
    if not ML_ENGINE_READY:
        raise HTTPException(503, "ml_engine not loaded")
    if employee_id.upper() not in ml_engine._models:
        await ml_load_model(employee_id.upper())

    conn = get_db()
    try:
        c = conn.cursor()
        c.execute("SELECT name FROM users WHERE employee_id=%s", (employee_id.upper(),))
        user = c.fetchone()
        if not user:
            raise HTTPException(404, "Employee not found")
        c.execute("""SELECT date,login_time,logout_time,hours_worked,status
                     FROM attendance WHERE employee_id=%s
                       AND date >= CURRENT_DATE - INTERVAL '%s days'
                       AND hours_worked IS NOT NULL ORDER BY date DESC""",
                  (employee_id.upper(), lookback_days))
        records = [dict(r) for r in c.fetchall()]
    finally:
        release_db(conn)

    result = await asyncio.to_thread(ml_engine.detect_anomalies, employee_id.upper(), records)
    result["name"] = user["name"]
    return result


@router.get("/api/ml/behaviour/site/{site_id}")
async def ml_behaviour_site(site_id: int,
                            current: TokenData = Depends(require_manager_or_hr)):
    if not ML_ENGINE_READY:
        raise HTTPException(503, "ml_engine not loaded")
    conn = get_db()
    try:
        c = conn.cursor()
        c.execute("SELECT employee_id,name FROM users WHERE site_id=%s AND role='labour'",
                  (site_id,))
        labourers = c.fetchall()
        if not labourers:
            return {"site_id": site_id, "employees_analyzed": 0, "high_risk": 0, "results": []}
        emp_ids = [lab["employee_id"] for lab in labourers]
        c.execute("""SELECT employee_id,date,login_time,logout_time,hours_worked
                     FROM attendance WHERE employee_id=ANY(%s)
                       AND date >= CURRENT_DATE - INTERVAL '60 days'
                       AND hours_worked IS NOT NULL ORDER BY employee_id,date DESC""",
                  (emp_ids,))
        recs_map: dict = {}
        for row in c.fetchall():
            recs_map.setdefault(row["employee_id"], []).append(dict(row))
    finally:
        release_db(conn)

    results = []
    for lab in labourers:
        if lab["employee_id"] not in ml_engine._models:
            await ml_load_model(lab["employee_id"])
        recs = recs_map.get(lab["employee_id"], [])
        r = await asyncio.to_thread(ml_engine.detect_anomalies, lab["employee_id"], recs)
        r["name"] = lab["name"]
        results.append(r)

    results.sort(key=lambda x: x["risk_score"], reverse=True)
    return {
        "site_id":            site_id,
        "employees_analyzed": len(results),
        "high_risk":          sum(1 for r in results if r["risk_label"] == "high"),
        "results":            results,
    }
