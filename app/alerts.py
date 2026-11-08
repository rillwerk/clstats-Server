"""Decide which push notifications a fresh set of forecasts should trigger."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

from .analytics import Forecast, window_id


@dataclass(frozen=True)
class Alert:
    key: str
    window: str
    kind: str
    title: str
    body: str
    silent: bool = False  # recorded as sent, but not pushed


def _clock(ts: float, tz: ZoneInfo) -> str:
    return datetime.fromtimestamp(ts, tz).strftime("%H:%M")


WEEKDAYS = ("Mo", "Di", "Mi", "Do", "Fr", "Sa", "So")


def _day_clock(ts: float, tz: ZoneInfo) -> str:
    dt = datetime.fromtimestamp(ts, tz)
    return f"{WEEKDAYS[dt.weekday()]} {dt:%H:%M}"


def alerts_for(forecasts: list[Forecast], thresholds: tuple[int, ...], tz: ZoneInfo) -> list[Alert]:
    out: list[Alert] = []
    for f in forecasts:
        if f.resets_at is None:
            continue
        win = window_id(f.resets_at)
        fmt = _clock if f.key == "five_hour" else _day_clock
        reset = fmt(f.resets_at, tz)
        # Only the highest threshold crossed; lower ones are marked too so they never fire late.
        crossed = [t for t in sorted(thresholds) if f.utilization >= t]
        if crossed:
            top = crossed[-1]
            for t in crossed:
                out.append(Alert(
                    f.key, win, f"threshold:{t}",
                    f"{f.label}: {round(f.utilization)} %",
                    f"Reset um {reset}.",
                    silent=t != top,
                ))
        if f.status == "limit_before_reset" and f.limit_eta:
            out.append(Alert(
                f.key, win, "forecast",
                f"{f.label}: Limit voraussichtlich um {fmt(f.limit_eta, tz)}",
                f"Bei aktuellem Tempo vor dem Reset um {reset}.",
            ))
    return out
