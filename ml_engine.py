"""
ml_engine.py — AttendTrack ML layer
====================================
Two models, both trained per-employee on real attendance data:

1. OvertimePredictor
   Input features: day_of_month, days_remaining, hours_so_far,
                   avg_daily_hours (rolling 30-day), attendance_rate_this_month
   Target:         projected month-end hours
   Model:          GradientBoostingRegressor (falls back to linear if < 5 samples)

2. AnomalyDetector  (behaviour / burnout signals)
   Input features per day-record: hours_worked, login_minute, logout_minute,
                                   day_of_week, hours_vs_personal_mean
   Model:          IsolationForest  (unsupervised — no labels needed)
   Output:         anomaly score per record + overall employee risk score

Models are stored in-memory (dict keyed by employee_id) and can be retrained
at any time via train_employee().  The scheduler calls retrain_all() nightly.

All public functions are synchronous — call them from async FastAPI handlers
with asyncio.to_thread() if needed (already done in main.py).
"""

import math
import json
from datetime import datetime, date
from typing import Optional

# ── Optional heavy deps — degrade gracefully ─────────────────────────────────
try:
    import numpy as np
    import joblib
    from sklearn.ensemble import GradientBoostingRegressor, IsolationForest
    from sklearn.linear_model import LinearRegression
    from sklearn.preprocessing import StandardScaler
    SK_READY = True
except ImportError:
    SK_READY = False

# ── In-memory model store ─────────────────────────────────────────────────────
# { employee_id: { "ot_model": ..., "ot_scaler": ...,
#                  "an_model": ..., "an_scaler": ...,
#                  "trained_at": datetime, "n_samples": int } }
_models: dict = {}

OVERTIME_LIMIT_HOURS = 105.0   # updated from env in init()

def init(overtime_limit: float):
    global OVERTIME_LIMIT_HOURS
    OVERTIME_LIMIT_HOURS = overtime_limit


# ─────────────────────────────────────────────────────────────────────────────
# FEATURE BUILDERS
# ─────────────────────────────────────────────────────────────────────────────

def _to_minutes(t) -> Optional[float]:
    """Convert a datetime / time-string to minute-of-day."""
    if t is None:
        return None
    s = str(t)
    import re
    m = re.search(r'(\d{2}):(\d{2})', s)
    if not m:
        return None
    return int(m.group(1)) * 60 + int(m.group(2))


def _ot_features(rows: list, as_of: datetime) -> list:
    """
    Build one overtime-prediction feature vector from a list of
    attendance row dicts (same month, sorted oldest-first).

    Returns: [day_of_month, days_remaining, hours_so_far,
               avg_daily_hours_30d, attendance_rate]
    """
    d = as_of.date()
    # days in current month
    if d.month == 12:
        from datetime import date as dt
        next_month_start = dt(d.year + 1, 1, 1)
    else:
        from datetime import date as dt
        next_month_start = dt(d.year, d.month + 1, 1)
    month_start = d.replace(day=1)
    days_in_month = (next_month_start - month_start).days
    days_elapsed  = (d - month_start).days + 1
    days_remaining = max(0, days_in_month - days_elapsed)

    hours_so_far = sum(float(r.get("hours_worked") or 0) for r in rows)
    n_days_worked = max(1, sum(1 for r in rows if r.get("hours_worked")))
    avg_daily = hours_so_far / n_days_worked
    attendance_rate = n_days_worked / max(1, days_elapsed)

    return [days_elapsed, days_remaining, hours_so_far, avg_daily, attendance_rate]


def _anomaly_features(row: dict, personal_mean: float) -> list:
    """
    Build one anomaly-detection feature vector from a single attendance record.

    Returns: [hours_worked, login_minute, logout_minute, day_of_week,
               hours_vs_mean, session_gap]
    """
    hw       = float(row.get("hours_worked") or 0)
    login_m  = _to_minutes(row.get("login_time")) or 510   # default 08:30
    logout_m = _to_minutes(row.get("logout_time")) or 1020  # default 17:00
    dow      = 0
    try:
        dow = datetime.fromisoformat(str(row.get("date"))[:10]).weekday()
    except Exception:
        pass
    gap = logout_m - login_m
    return [hw, login_m, logout_m, dow, hw - personal_mean, gap]


# ─────────────────────────────────────────────────────────────────────────────
# TRAINING
# ─────────────────────────────────────────────────────────────────────────────

def train_employee(employee_id: str, all_records: list) -> dict:
    """
    Train (or retrain) both models for one employee.

    `all_records` — list of attendance dicts from the DB, any date range.
    Returns a status dict.
    """
    if not SK_READY:
        return {"ok": False, "reason": "scikit-learn not installed"}

    # Sort oldest first
    records = sorted(
        [r for r in all_records if r.get("hours_worked") is not None],
        key=lambda r: str(r.get("date", ""))
    )
    if len(records) < 3:
        return {"ok": False, "reason": f"only {len(records)} complete records — need ≥ 3 to train"}

    # ── Overtime prediction model ────────────────────────────────────────────
    # We generate training samples by simulating "mid-month snapshots":
    # for each record index i (>= 2), use records[:i] as "data so far"
    # and the actual month total as the target.
    ot_X, ot_y = [], []
    month_groups: dict = {}
    for r in records:
        key = str(r.get("date", ""))[:7]   # "YYYY-MM"
        month_groups.setdefault(key, []).append(r)

    for month_key, month_rows in month_groups.items():
        if len(month_rows) < 2:
            continue
        month_total = sum(float(r.get("hours_worked") or 0) for r in month_rows)
        # simulate snapshots at each day within the month
        for i in range(1, len(month_rows)):
            snap_rows = month_rows[:i]
            # fake "as_of" = the date of the last snap row
            try:
                snap_date = datetime.fromisoformat(str(month_rows[i - 1]["date"])[:10])
            except Exception:
                continue
            feats = _ot_features(snap_rows, snap_date)
            ot_X.append(feats)
            ot_y.append(month_total)

    ot_model = None
    ot_scaler = None
    if len(ot_X) >= 5:
        ot_X_np = np.array(ot_X, dtype=float)
        ot_y_np = np.array(ot_y, dtype=float)
        ot_scaler = StandardScaler()
        ot_X_sc = ot_scaler.fit_transform(ot_X_np)
        try:
            ot_model = GradientBoostingRegressor(
                n_estimators=80, max_depth=3, learning_rate=0.1,
                subsample=0.8, random_state=42
            )
            ot_model.fit(ot_X_sc, ot_y_np)
        except Exception:
            ot_model = LinearRegression()
            ot_model.fit(ot_X_sc, ot_y_np)
    else:
        # Fallback linear on tiny datasets
        if len(ot_X) >= 2:
            ot_X_np = np.array(ot_X, dtype=float)
            ot_y_np = np.array(ot_y, dtype=float)
            ot_scaler = StandardScaler()
            ot_X_sc = ot_scaler.fit_transform(ot_X_np)
            ot_model = LinearRegression()
            ot_model.fit(ot_X_sc, ot_y_np)

    # ── Anomaly detection model ──────────────────────────────────────────────
    personal_mean = sum(float(r.get("hours_worked") or 0) for r in records) / len(records)
    an_X = [_anomaly_features(r, personal_mean) for r in records]
    an_model  = None
    an_scaler = None
    if len(an_X) >= 5:
        an_X_np = np.array(an_X, dtype=float)
        an_scaler = StandardScaler()
        an_X_sc = an_scaler.fit_transform(an_X_np)
        # contamination = expected fraction of anomalies; 0.1 = 10 %
        an_model = IsolationForest(
            n_estimators=100, contamination=0.1,
            random_state=42, n_jobs=1
        )
        an_model.fit(an_X_sc)

    _models[employee_id] = {
        "ot_model":     ot_model,
        "ot_scaler":    ot_scaler,
        "an_model":     an_model,
        "an_scaler":    an_scaler,
        "personal_mean": personal_mean,
        "trained_at":   datetime.now().isoformat(),
        "n_samples":    len(records),
    }
    return {
        "ok": True,
        "employee_id": employee_id,
        "ot_samples": len(ot_X),
        "an_samples": len(an_X),
        "trained_at": _models[employee_id]["trained_at"],
    }


def retrain_all(employee_records_map: dict) -> dict:
    """
    Batch retrain.  `employee_records_map` = { employee_id: [records] }.
    Returns summary dict.
    """
    results = {}
    for eid, recs in employee_records_map.items():
        results[eid] = train_employee(eid, recs)
    ok  = sum(1 for r in results.values() if r.get("ok"))
    nok = len(results) - ok
    return {"retrained": ok, "skipped": nok, "details": results}


# ─────────────────────────────────────────────────────────────────────────────
# INFERENCE
# ─────────────────────────────────────────────────────────────────────────────

def predict_overtime(employee_id: str, current_month_records: list, as_of: datetime) -> dict:
    """
    Predict projected month-end hours for an employee.
    Falls back to linear arithmetic if no model is trained yet.
    """
    feats = _ot_features(current_month_records, as_of)
    hours_so_far = feats[2]

    mdl = _models.get(employee_id)
    if mdl and mdl.get("ot_model") and SK_READY:
        try:
            X = np.array([feats], dtype=float)
            X_sc = mdl["ot_scaler"].transform(X)
            predicted = float(mdl["ot_model"].predict(X_sc)[0])
            predicted = max(hours_so_far, round(predicted, 1))
            return {
                "employee_id":          employee_id,
                "hours_so_far":         round(hours_so_far, 1),
                "projected_month_end":  predicted,
                "will_exceed_limit":    predicted >= OVERTIME_LIMIT_HOURS,
                "method":               "ml_gradient_boosting",
                "model_samples":        mdl.get("n_samples", 0),
                "trained_at":           mdl.get("trained_at"),
            }
        except Exception as e:
            pass  # fall through to linear fallback

    # Linear fallback: hours_so_far / days_elapsed * days_in_month
    days_elapsed   = feats[0]
    days_remaining = feats[1]
    avg_daily      = feats[3]
    projected      = round(hours_so_far + avg_daily * days_remaining, 1)
    return {
        "employee_id":         employee_id,
        "hours_so_far":        round(hours_so_far, 1),
        "projected_month_end": projected,
        "will_exceed_limit":   projected >= OVERTIME_LIMIT_HOURS,
        "method":              "linear_fallback",
        "model_samples":       0,
        "trained_at":          None,
    }


def detect_anomalies(employee_id: str, records: list) -> dict:
    """
    Score each record in `records` for anomaly.
    Returns an overall risk score (0–1) and per-record flags.
    """
    if not SK_READY or not records:
        return {
            "employee_id":  employee_id,
            "risk_score":   0.0,
            "risk_label":   "unknown",
            "flags":        [],
            "method":       "no_ml" if not SK_READY else "no_records",
        }

    mdl = _models.get(employee_id)
    personal_mean = mdl["personal_mean"] if mdl else (
        sum(float(r.get("hours_worked") or 0) for r in records) / max(1, len(records))
    )

    flags = []
    anomaly_count = 0

    if mdl and mdl.get("an_model") and SK_READY:
        try:
            X = np.array([_anomaly_features(r, personal_mean) for r in records], dtype=float)
            X_sc = mdl["an_scaler"].transform(X)
            scores = mdl["an_model"].decision_function(X_sc)   # higher = more normal
            preds  = mdl["an_model"].predict(X_sc)             # -1 = anomaly, 1 = normal
            for i, (r, pred, score) in enumerate(zip(records, preds, scores)):
                is_anom = int(pred) == -1
                if is_anom:
                    anomaly_count += 1
                    hw   = float(r.get("hours_worked") or 0)
                    diff = abs(hw - personal_mean)
                    reason = _anomaly_reason(r, personal_mean)
                    flags.append({
                        "date":   str(r.get("date", ""))[:10],
                        "anomaly_score": round(float(-score), 3),
                        "hours_worked":  round(hw, 2),
                        "reason": reason,
                    })
        except Exception:
            pass

    if not flags:
        # Rule-based fallback when model isn't trained or failed
        for r in records:
            hw = float(r.get("hours_worked") or 0)
            login_m  = _to_minutes(r.get("login_time"))
            logout_m = _to_minutes(r.get("logout_time"))
            reasons  = []
            if hw > personal_mean * 1.5 and hw > 10:
                reasons.append(f"unusually long shift ({hw:.1f}h vs personal avg {personal_mean:.1f}h)")
            if hw < personal_mean * 0.4 and personal_mean > 4:
                reasons.append(f"unusually short shift ({hw:.1f}h vs avg {personal_mean:.1f}h)")
            if login_m is not None and login_m > 600:   # later than 10:00
                reasons.append(f"very late start ({login_m//60:02d}:{login_m%60:02d})")
            if logout_m is not None and logout_m < 840: # before 14:00
                reasons.append(f"very early exit ({logout_m//60:02d}:{logout_m%60:02d})")
            if reasons:
                anomaly_count += 1
                flags.append({
                    "date":          str(r.get("date", ""))[:10],
                    "anomaly_score": 0.5,
                    "hours_worked":  round(hw, 2),
                    "reason":        "; ".join(reasons),
                })

    total = len(records)
    risk  = round(anomaly_count / max(1, total), 3)
    label = "high" if risk > 0.3 else ("medium" if risk > 0.1 else "low")

    return {
        "employee_id":  employee_id,
        "risk_score":   risk,
        "risk_label":   label,
        "anomaly_days": anomaly_count,
        "total_days":   total,
        "flags":        flags[:10],   # cap at 10 for payload size
        "method":       "isolation_forest" if (mdl and mdl.get("an_model")) else "rule_fallback",
    }


def _anomaly_reason(row: dict, personal_mean: float) -> str:
    hw      = float(row.get("hours_worked") or 0)
    login_m = _to_minutes(row.get("login_time"))
    reasons = []
    if hw > personal_mean * 1.5 and hw > 10:
        reasons.append(f"unusually long ({hw:.1f}h, avg {personal_mean:.1f}h)")
    if hw < personal_mean * 0.4 and personal_mean > 3:
        reasons.append(f"unusually short ({hw:.1f}h, avg {personal_mean:.1f}h)")
    if login_m and login_m > 600:
        reasons.append(f"late start {login_m//60:02d}:{login_m%60:02d}")
    return "; ".join(reasons) or "statistical outlier"
