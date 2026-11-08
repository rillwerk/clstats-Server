"""Derived numbers: pace, forecast, consumption, sessions, heatmap.

The API only reports the current utilization and the reset time of each window,
so everything here is computed from the samples the collector stored.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

from .db import Sample

HOUR = 3600.0
SESSION_WINDOW = 5 * HOUR
WEEK_WINDOW = 7 * 24 * HOUR
# resets_at jitters by a few seconds between fetches; anything closer than this is the same window.
WINDOW_TOLERANCE = 10 * 60


def window_length(key: str) -> float:
    return SESSION_WINDOW if key == "five_hour" else WEEK_WINDOW


def forecast_lookback(key: str) -> float:
    # A session burns fast and in bursts: use the last hour. A week: the last day.
    return HOUR if key == "five_hour" else 24 * HOUR


def same_window(a: float | None, b: float | None) -> bool:
    if a is None or b is None:
        return a is None and b is None
    return abs(a - b) <= WINDOW_TOLERANCE


def window_id(resets_at: float | None) -> str:
    return "none" if resets_at is None else str(int(round(resets_at / 600) * 600))


@dataclass
class Forecast:
    key: str
    label: str
    utilization: float
    resets_at: float | None
    pace: float | None             # where utilization "should" be if spread evenly, percent
    rate_per_hour: float           # recent burn rate, percent per hour
    projected_at_reset: float | None
    limit_eta: float | None        # unix seconds when 100 % is reached at the current rate
    status: str                    # idle | ok | tight | limit_before_reset | limit_reached

    def to_dict(self) -> dict:
        return asdict(self)


def linear_rate(points: list[tuple[float, float]]) -> float:
    """Least-squares slope in percent per hour."""
    if len(points) < 2:
        return 0.0
    n = len(points)
    mx = sum(t for t, _ in points) / n
    my = sum(u for _, u in points) / n
    var = sum((t - mx) ** 2 for t, _ in points)
    if var == 0:
        return 0.0
    cov = sum((t - mx) * (u - my) for t, u in points)
    return max(0.0, cov / var * HOUR)


def forecast(key_samples: list[Sample], now: float) -> Forecast | None:
    if not key_samples:
        return None
    last = key_samples[-1]
    window = window_length(last.key)
    pace = None
    if last.resets_at and now < last.resets_at:
        elapsed = now - (last.resets_at - window)
        pace = max(0.0, min(100.0, elapsed / window * 100))

    lookback = now - forecast_lookback(last.key)
    current = [s for s in key_samples if same_window(s.resets_at, last.resets_at) and s.ts >= lookback]
    points = [(s.ts, s.utilization) for s in current]
    span = points[-1][0] - points[0][0] if len(points) > 1 else 0
    rate = linear_rate(points) if span >= 10 * 60 else 0.0

    projected = eta = None
    if last.resets_at and now < last.resets_at:
        projected = last.utilization + rate * (last.resets_at - now) / HOUR
    if rate > 0 and last.utilization < 100:
        eta = now + (100 - last.utilization) / rate * HOUR

    if last.utilization >= 100:
        status = "limit_reached"
    elif last.resets_at is None or last.utilization == 0 and rate == 0:
        status = "idle"
    elif projected is not None and projected >= 100:
        status = "limit_before_reset"
    elif projected is not None and projected >= 85:
        status = "tight"
    else:
        status = "ok"
    return Forecast(last.key, last.label, last.utilization, last.resets_at, pace, rate, projected, eta, status)


def deltas(key_samples: list[Sample]) -> list[tuple[float, float]]:
    """Consumption per sample interval: (timestamp, percent consumed since previous sample)."""
    out: list[tuple[float, float]] = []
    prev: Sample | None = None
    for s in key_samples:
        if prev is None:
            prev = s
            continue
        if same_window(s.resets_at, prev.resets_at) and s.utilization >= prev.utilization:
            d = s.utilization - prev.utilization
        elif same_window(s.resets_at, prev.resets_at):
            d = 0.0  # small downward corrections inside a window
        else:
            d = s.utilization  # a new window started from zero
        if d > 0:
            out.append((s.ts, d))
        prev = s
    return out


def daily(key_samples: list[Sample], tz: ZoneInfo) -> list[dict]:
    per_day: dict[str, float] = defaultdict(float)
    for ts, d in deltas(key_samples):
        per_day[datetime.fromtimestamp(ts, tz).date().isoformat()] += d
    return [{"date": day, "consumed": round(v, 2)} for day, v in sorted(per_day.items())]


def heatmap(key_samples: list[Sample], tz: ZoneInfo) -> list[dict]:
    """Consumption by weekday (0 = Monday) and hour of day."""
    cells: dict[tuple[int, int], float] = defaultdict(float)
    for ts, d in deltas(key_samples):
        dt = datetime.fromtimestamp(ts, tz)
        cells[(dt.weekday(), dt.hour)] += d
    return [{"weekday": w, "hour": h, "consumed": round(v, 2)} for (w, h), v in sorted(cells.items())]


def windows(key_samples: list[Sample]) -> list[dict]:
    """One entry per usage window (session or week) with its peak and whether it ended early."""
    out: list[dict] = []
    for s in key_samples:
        if s.resets_at is None:
            continue
        if out and same_window(out[-1]["resets_at"], s.resets_at):
            w = out[-1]
            w["peak"] = max(w["peak"], s.utilization)
            w["last_seen"] = s.ts
            continue
        if out and out[-1]["resets_at"] > s.ts + WINDOW_TOLERANCE:
            out[-1]["early_reset"] = True
        out.append({
            "resets_at": s.resets_at,
            "start": s.resets_at - window_length(s.key),
            "first_seen": s.ts,
            "last_seen": s.ts,
            "peak": s.utilization,
            "early_reset": False,
        })
    for w in out:
        w["hit_limit"] = w["peak"] >= 100
    return out


def summary(samples_by_key: dict[str, list[Sample]], tz: ZoneInfo, now: float, days: int = 30) -> dict:
    since = now - days * 24 * HOUR
    sessions = [w for w in windows(samples_by_key.get("five_hour", [])) if w["resets_at"] >= since]
    week_daily = [d for d in daily(samples_by_key.get("seven_day", []), tz)
                  if d["date"] >= datetime.fromtimestamp(since, tz).date().isoformat()]
    busiest = max(week_daily, key=lambda d: d["consumed"], default=None)
    active_days = [d for d in week_daily if d["consumed"] > 0]
    return {
        "days": days,
        "sessions": len(sessions),
        "sessions_hit_limit": sum(1 for w in sessions if w["hit_limit"]),
        "avg_session_peak": round(sum(w["peak"] for w in sessions) / len(sessions), 1) if sessions else None,
        "avg_weekly_per_active_day": round(sum(d["consumed"] for d in active_days) / len(active_days), 2)
        if active_days else None,
        "active_days": len(active_days),
        "busiest_day": busiest,
    }
