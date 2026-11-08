"""HTTP API for the iOS app, the widgets and the web dashboard."""

from __future__ import annotations

import csv
import io
import logging
import secrets
import time
from collections import defaultdict
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from datetime import time as dt_time
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field

from . import analytics, tokens
from .apns import APNs
from .collector import Collector
from .config import Settings, load_settings
from .db import Database, Sample

STATIC = Path(__file__).parent / "static"
MAX_POINTS = 600


class Credentials(BaseModel):
    cookie: str = Field(min_length=10)
    org_id: str | None = None


class TokenRowIn(BaseModel):
    date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    project: str = Field(min_length=1, max_length=200)
    model: str = Field(min_length=1, max_length=100)
    input: int = Field(0, ge=0)
    output: int = Field(0, ge=0)
    cache_write: int = Field(0, ge=0)
    cache_write_1h: int = Field(0, ge=0)
    cache_read: int = Field(0, ge=0)
    messages: int = Field(0, ge=0)
    slot_start: int | None = Field(None, ge=0)  # 15-minute slot, unix seconds; missing in old scripts


class TokenReport(BaseModel):
    source: str = Field(min_length=1, max_length=100)
    rows: list[TokenRowIn] = Field(max_length=100_000)
    # Set by newer scripts: their rows are complete from this 15-minute slot on, so the
    # server drops the source's older rows there (e.g. under a project name that changed).
    replace_from: int | None = Field(None, ge=0)


class Device(BaseModel):
    token: str = Field(min_length=16, max_length=200)


class AuthFailures:
    """Slows down token guessing: after too many wrong tokens a client gets 429 for a while.

    Per client address, plus a global cap in case addresses are spoofed via X-Forwarded-For.
    """

    def __init__(self, per_client: int = 10, overall: int = 100, window: float = 600):
        self.per_client, self.overall, self.window = per_client, overall, window
        self._events: dict[str, list[float]] = defaultdict(list)
        self._all: list[float] = []

    def _trim(self, now: float) -> None:
        cutoff = now - self.window
        self._all = [t for t in self._all if t > cutoff]
        for key in list(self._events):
            kept = [t for t in self._events[key] if t > cutoff]
            if kept:
                self._events[key] = kept
            else:
                del self._events[key]

    def blocked(self, client: str, now: float | None = None) -> bool:
        now = now or time.time()
        self._trim(now)
        return len(self._events.get(client, ())) >= self.per_client or len(self._all) >= self.overall

    def add(self, client: str, now: float | None = None) -> None:
        now = now or time.time()
        self._events[client].append(now)
        self._all.append(now)


def create_app(settings: Settings | None = None, collector: Collector | None = None) -> FastAPI:
    settings = settings or load_settings()
    if collector is None:
        collector = Collector(settings, Database(settings.db_path), APNs(settings))
        start_thread = True
    else:
        start_thread = False
    db = collector.db
    tz = settings.timezone
    last_manual_refresh = [0.0]

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        if start_thread:
            collector.start()
        yield
        collector.stop()

    app = FastAPI(title="ClStats Server", lifespan=lifespan)

    failures = AuthFailures()

    def auth(
        request: Request,
        authorization: str | None = Header(default=None),
        access_token: str | None = Query(default=None),
    ) -> None:
        client = request.client.host if request.client else "unknown"
        if failures.blocked(client):
            raise HTTPException(status_code=429, detail="too many failed attempts, try again later")
        supplied = access_token
        if authorization and authorization.lower().startswith("bearer "):
            supplied = authorization[7:]
        if not supplied or not secrets.compare_digest(supplied, settings.api_token):
            failures.add(client)
            raise HTTPException(status_code=401, detail="invalid token")

    def by_key(since: float) -> dict[str, list[Sample]]:
        out: dict[str, list[Sample]] = defaultdict(list)
        for s in db.samples(since=since):
            out[s.key].append(s)
        return out

    def current_payload() -> dict:
        now = time.time()
        latest = db.latest()
        return {
            "now": now,
            "updated_at": latest[0].ts if latest else None,
            "status": collector.status(),
            "limits": [f.to_dict() for f in collector.forecasts(now)],
        }

    @app.get("/api/health")
    def health() -> dict:
        return {"ok": True}

    @app.get("/api/current", dependencies=[Depends(auth)])
    def current() -> dict:
        return current_payload()

    @app.post("/api/refresh", dependencies=[Depends(auth)])
    def refresh() -> dict:
        # Manual refreshes are capped so a widget or a stuck client cannot hammer claude.ai.
        if time.time() - last_manual_refresh[0] >= 60:
            last_manual_refresh[0] = time.time()
            collector.run_once()
        return current_payload()

    @app.post("/api/credentials", dependencies=[Depends(auth)])
    def set_credentials(body: Credentials) -> dict:
        collector.set_cookie(body.cookie, body.org_id)
        collector.run_once()
        return current_payload()

    @app.delete("/api/credentials", dependencies=[Depends(auth)])
    def clear_credentials() -> dict:
        collector.clear_cookie()
        return collector.status()

    @app.post("/api/ingest", dependencies=[Depends(auth)])
    def ingest(body: dict) -> dict:
        """Fallback when the server itself cannot reach claude.ai: the app posts the raw /usage JSON."""
        if not collector.ingest(body.get("usage") or {}):
            raise HTTPException(status_code=422, detail="no limits in payload")
        return current_payload()

    @app.get("/api/history", dependencies=[Depends(auth)])
    def history(key: str = "five_hour", hours: float = Query(24, gt=0, le=24 * 400)) -> dict:
        samples = db.samples(key=key, since=time.time() - hours * 3600)
        step = max(1, len(samples) // MAX_POINTS)
        points = samples[::step]
        if samples and points[-1] is not samples[-1]:
            points.append(samples[-1])
        return {
            "key": key,
            "points": [{"ts": s.ts, "utilization": s.utilization, "resets_at": s.resets_at} for s in points],
        }

    @app.get("/api/daily", dependencies=[Depends(auth)])
    def daily(key: str = "seven_day", days: int = Query(30, gt=0, le=400)) -> dict:
        samples = db.samples(key=key, since=time.time() - days * 86400)
        return {"key": key, "days": analytics.daily(samples, tz)}

    @app.get("/api/heatmap", dependencies=[Depends(auth)])
    def heatmap(key: str = "five_hour", days: int = Query(28, gt=0, le=400)) -> dict:
        samples = db.samples(key=key, since=time.time() - days * 86400)
        return {"key": key, "days": days, "cells": analytics.heatmap(samples, tz)}

    @app.get("/api/windows", dependencies=[Depends(auth)])
    def windows(key: str = "five_hour", days: int = Query(30, gt=0, le=400)) -> dict:
        samples = db.samples(key=key, since=time.time() - days * 86400)
        return {"key": key, "windows": list(reversed(analytics.windows(samples)))}

    @app.get("/api/summary", dependencies=[Depends(auth)])
    def summary(days: int = Query(30, gt=0, le=400)) -> dict:
        now = time.time()
        return analytics.summary(by_key(now - days * 86400), tz, now, days)

    @app.post("/api/tokens", dependencies=[Depends(auth)])
    def add_tokens(body: TokenReport) -> dict:
        """Totals from Claude Code's local logs, sent by mac/claude_tokens.py."""
        source = db.resolve_source(body.source)
        rows = [tokens.TokenRow(source=source, **r.model_dump()) for r in body.rows]
        db.upsert_tokens(rows, time.time(), replace_from=body.replace_from)
        return {"ok": True, "rows": len(rows)}

    @app.get("/api/tokens", dependencies=[Depends(auth)])
    def get_tokens(
        days: int = Query(30, gt=0, le=400),
        hours: int | None = Query(None, gt=0, le=72),
    ) -> dict:
        """Tokens per project for the last `hours` (15-minute resolution, bars per hour)
        or the last `days` (bars per day). `hours` wins when both are given."""
        now = time.time()
        if hours:
            since_slot = int(now - hours * 3600) // tokens.SLOT * tokens.SLOT
            rows = db.token_rows(since_slot, since_date=None)
            meta = {"hours": hours, "days": None, "since": datetime.fromtimestamp(since_slot, tz).isoformat()}
            bucket = lambda r: r.slot_start // 3600 * 3600
        else:
            start = datetime.combine(datetime.now(tz).date() - timedelta(days=days - 1), dt_time(), tz)
            since = start.date().isoformat()
            # Slots are filtered by the Mac's own date so they line up with old per-day rows.
            rows = [r for r in db.token_rows(int(start.timestamp()) - 86400, since) if r.date >= since]
            meta = {"hours": None, "days": days, "since": since}

            def bucket(r: tokens.TokenRow) -> int:
                day = datetime.strptime(r.date, "%Y-%m-%d").replace(tzinfo=tz)
                return int(day.timestamp())

        out = tokens.report(rows, bucket, **meta)
        sources = db.token_sources()
        out["sources"] = sources
        out["updated_at"] = max((s["updated_at"] for s in sources), default=None)
        return out

    @app.post("/api/devices", dependencies=[Depends(auth)])
    def add_device(body: Device) -> dict:
        db.add_device(body.token, time.time())
        return {"ok": True}

    @app.delete("/api/devices/{token}", dependencies=[Depends(auth)])
    def remove_device(token: str) -> dict:
        db.remove_device(token)
        return {"ok": True}

    @app.get("/api/export.csv", dependencies=[Depends(auth)])
    def export() -> StreamingResponse:
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["timestamp_utc", "key", "label", "utilization", "resets_at_utc"])
        iso = lambda t: time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t)) if t else ""
        for s in db.samples():
            w.writerow([iso(s.ts), s.key, s.label, s.utilization, iso(s.resets_at)])
        buf.seek(0)
        return StreamingResponse(
            iter([buf.getvalue()]),
            media_type="text/csv",
            headers={"Content-Disposition": "attachment; filename=claude-usage.csv"},
        )

    @app.get("/privacy", include_in_schema=False)
    def privacy() -> FileResponse:
        """What this server stores, public and without token (the operator is responsible for it)."""
        return FileResponse(STATIC / "privacy.html")

    @app.get("/", include_in_schema=False)
    def dashboard() -> FileResponse:
        return FileResponse(STATIC / "index.html")

    return app


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


def app_factory() -> FastAPI:
    return create_app()
