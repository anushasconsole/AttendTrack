"""
app/services/overtime.py — Monthly overtime checking and WhatsApp alerts.
"""
import math
from datetime import datetime
from app.core.config import OVERTIME_LIMIT_HOURS, OVERTIME_WARN_AT
from app.services.whatsapp import send_whatsapp_alert


def _month_bounds(d: datetime):
    start = d.replace(day=1)
    if d.month == 12:
        end = d.replace(year=d.year + 1, month=1, day=1)
    else:
        end = d.replace(month=d.month + 1, day=1)
    return start.date(), end.date()


def check_and_alert_overtime(c, employee_id: str, phone: str, name: str,
                              as_of: datetime) -> dict:
    """
    Sum this calendar month's approved hours. If thresholds are crossed,
    send a WhatsApp alert — but only once per employee per period per level
    (guarded by a UNIQUE DB constraint on overtime_alerts).
    """
    period_start, period_end = _month_bounds(as_of)
    c.execute(
        """SELECT COALESCE(SUM(hours_worked),0) as total FROM attendance
           WHERE employee_id=%s AND date >= %s AND date < %s
             AND hours_worked IS NOT NULL""",
        (employee_id, period_start, period_end),
    )
    total_hours = float(c.fetchone()["total"])

    warn_threshold = OVERTIME_LIMIT_HOURS * OVERTIME_WARN_AT
    level = None
    if total_hours >= OVERTIME_LIMIT_HOURS:
        level = "exceeded"
    elif total_hours >= warn_threshold:
        level = "warning"

    if not level:
        return {"alert_fired": False, "total_hours": total_hours}

    c.execute(
        """SELECT 1 FROM overtime_alerts
           WHERE employee_id=%s AND period_start=%s AND alert_level=%s""",
        (employee_id, period_start, level),
    )
    if c.fetchone():
        return {"alert_fired": False, "total_hours": total_hours,
                "reason": "already_sent_this_period"}

    if level == "exceeded":
        message = (
            f"⚠️ AttendTrack: {name}, you have worked {total_hours:.1f} hours this month, "
            f"exceeding the {OVERTIME_LIMIT_HOURS:.0f}-hour limit. Please contact your manager."
        )
    else:
        message = (
            f"AttendTrack: {name}, you have worked {total_hours:.1f} of "
            f"{OVERTIME_LIMIT_HOURS:.0f} allowed hours this month. You're approaching the limit."
        )

    result = send_whatsapp_alert(phone, message)
    c.execute(
        """INSERT INTO overtime_alerts
               (employee_id,period_start,period_end,hours_total,alert_level,sent_via,sent_ok)
           VALUES (%s,%s,%s,%s,%s,%s,%s)
           ON CONFLICT (employee_id,period_start,alert_level) DO NOTHING""",
        (employee_id, period_start, period_end, total_hours, level,
         "whatsapp", result.get("sent", False)),
    )
    return {"alert_fired": True, "level": level, "total_hours": total_hours,
            "send_result": result}
