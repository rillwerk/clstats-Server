"""HTTP API for the iOS app, the widgets and the web dashboard."""

from __future__ import annotations

import base64
import csv
import hashlib
import io
import logging
import re
import secrets
import threading
import time
from collections import defaultdict
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from datetime import time as dt_time
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, Field, field_validator

from . import analytics, tokens
from .apns import APNs
from .collector import Collector
from .config import Settings, load_settings
from .db import Database, Sample

STATIC = Path(__file__).parent / "static"
MAX_POINTS = 600
# The token script sends its full stock of 15-minute rows; even years of heavy use stay far
# below this. Larger requests are refused before anything reads the body.
MAX_BODY = 16 * 1024 * 1024


UUID_PATTERN = r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"


class Credentials(BaseModel):
    cookie: str = Field(min_length=10)
    # claude.ai organization ids are UUIDs; anything else would end up in the request path.
    org_id: str | None = Field(None, pattern=UUID_PATTERN)

    @field_validator("cookie")
    @classmethod
    def printable_ascii(cls, v: str) -> str:
        # The cookie goes into an HTTP header. httpx rejects control characters there with
        # an error that quotes the whole header value, which would then end up in the status
        # and the log. Refusing it here keeps it out of both.
        if not all(" " <= c <= "~" for c in v):
            raise ValueError("cookie may only contain printable ASCII characters")
        return v


def parse_day(text: str) -> datetime | None:
    """YYYY-MM-DD as a date, None for anything else (e.g. 2026-99-99)."""
    try:
        return datetime.strptime(text, "%Y-%m-%d")
    except (TypeError, ValueError):
        return None


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

    @field_validator("date")
    @classmethod
    def real_date(cls, v: str) -> str:
        # The pattern alone lets 2026-99-99 through, which later breaks every days view.
        if parse_day(v) is None:
            raise ValueError("not a calendar date")
        return v


class TokenReport(BaseModel):
    source: str = Field(min_length=1, max_length=100)
    rows: list[TokenRowIn] = Field(max_length=100_000)
    # Set by newer scripts: their rows are complete from this 15-minute slot on, so the
    # server drops the source's older rows there (e.g. under a project name that changed).
    replace_from: int | None = Field(None, ge=0)


class UsagePayload(BaseModel):
    """Body of /api/ingest: the raw claude.ai /usage JSON under "usage"."""
    usage: dict | None = None


class Device(BaseModel):
    token: str = Field(min_length=16, max_length=200)


class AuthFailures:
    """Slows down token guessing: after too many wrong tokens a client gets 429 for a while.

    Per client address, plus a global cap in case addresses are spoofed via X-Forwarded-For.
    Only wrong tokens are answered with 429: the token has at least 24 characters, so the
    block is defense in depth, and it must not lock out the owner when a scanner, an old
    script with a stale token or a shared address (NAT, reverse proxy) runs into it.
    """

    def __init__(self, per_client: int = 10, overall: int = 100, window: float = 600):
        self.per_client, self.overall, self.window = per_client, overall, window
        self._events: dict[str, list[float]] = defaultdict(list)
        self._all: list[float] = []
        # Requests are handled on several threads; the lists must not change mid-trim.
        self._lock = threading.Lock()

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
        now = time.time() if now is None else now
        with self._lock:
            self._trim(now)
            return len(self._events.get(client, ())) >= self.per_client or len(self._all) >= self.overall

    def retry_after(self, client: str, now: float | None = None) -> int:
        """Seconds until the block for this client lifts (at least 1)."""
        now = time.time() if now is None else now
        with self._lock:
            self._trim(now)
            waits = []
            own = self._events.get(client, [])
            if len(own) >= self.per_client:
                waits.append(own[-self.per_client] + self.window - now)
            if len(self._all) >= self.overall:
                waits.append(self._all[-self.overall] + self.window - now)
        return max(1, int(max(waits, default=1)) + 1)

    def add(self, client: str, now: float | None = None) -> None:
        now = time.time() if now is None else now
        with self._lock:
            self._events[client].append(now)
            self._all.append(now)


def dashboard_csp() -> str:
    """Content-Security-Policy for the HTML pages: own scripts only (Chart.js served from
    /static, the inline scripts by their hash), no framing, no foreign connections."""
    hashes = []
    for page in ("index.html", "privacy.html"):
        html = (STATIC / page).read_text(encoding="utf-8")
        for body in re.findall(r"<script>(.*?)</script>", html, flags=re.S):
            digest = base64.b64encode(hashlib.sha256(body.encode("utf-8")).digest()).decode()
            hashes.append(f"'sha256-{digest}'")
    return "; ".join([
        "default-src 'self'",
        "script-src 'self' " + " ".join(hashes),
        "style-src 'self' 'unsafe-inline'",
        "img-src 'self' data:",
        "connect-src 'self'",
        "frame-ancestors 'none'",
        "base-uri 'none'",
        "form-action 'self'",
        "object-src 'none'",
    ])


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

    # No /docs, /redoc or /openapi.json: they would describe the API to anyone without a token.
    app = FastAPI(title="ClStats Server", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    failures = AuthFailures()

    csp = dashboard_csp()

    ingest_tokens = (settings.api_token,) + ((settings.ingest_token,) if settings.ingest_token else ())

    def error(status: int, detail: str, headers: dict[str, str] | None = None) -> JSONResponse:
        # Same shape as FastAPI's own HTTPException answers.
        return JSONResponse({"detail": detail}, status_code=status, headers=headers)

    def check_token(request: Request) -> JSONResponse | None:
        """The one token check for /api/ (except /api/health): None when the request may pass.

        POST /api/tokens also takes the narrower INGEST_TOKEN, everything else only API_TOKEN.
        Only the Authorization header counts: a token in the URL would end up in access logs
        (uvicorn, reverse proxy, Cloudflare) and in the browser history.
        """
        if request.method == "POST" and request.url.path == "/api/tokens":
            accepted = ingest_tokens
        else:
            accepted = (settings.api_token,)
        client = request.client.host if request.client else "unknown"
        authorization = request.headers.get("authorization")
        supplied = None
        if authorization and authorization.lower().startswith("bearer "):
            supplied = authorization[7:]
        if supplied and any(secrets.compare_digest(supplied, t) for t in accepted):
            return None
        if failures.blocked(client):
            return error(429, "too many failed attempts, try again later",
                         {"Retry-After": str(failures.retry_after(client))})
        failures.add(client)
        return error(401, "invalid token")

    # Registered before security_headers, so it runs inside it and its answers get the headers too.
    @app.middleware("http")
    async def require_token(request: Request, call_next):
        # Runs before routing and before anything reads the body: without a valid token a
        # client gets 401 (not a validation error that reveals the endpoint's schema), and
        # nobody without a token can make the server parse a large body.
        length = request.headers.get("content-length")
        if length and length.isdigit() and int(length) > MAX_BODY:
            return error(413, "request body too large")
        path = request.url.path
        if path.startswith("/api/") and path != "/api/health":
            denied = check_token(request)
            if denied is not None:
                return denied
        return await call_next(request)

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault("X-Frame-Options", "DENY")
        if response.headers.get("content-type", "").startswith("text/html"):
            response.headers.setdefault("Content-Security-Policy", csp)
        return response

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

    @app.get("/api/current")
    def current() -> dict:
        return current_payload()

    @app.post("/api/refresh")
    def refresh() -> dict:
        # Manual refreshes are capped so a widget or a stuck client cannot hammer claude.ai.
        if time.time() - last_manual_refresh[0] >= 60:
            last_manual_refresh[0] = time.time()
            collector.run_once()
        return current_payload()

    @app.post("/api/credentials")
    def set_credentials(body: Credentials) -> dict:
        collector.set_cookie(body.cookie, body.org_id)
        collector.run_once()
        return current_payload()

    @app.delete("/api/credentials")
    def clear_credentials() -> dict:
        collector.clear_cookie()
        return collector.status()

    @app.post("/api/ingest")
    def ingest(body: UsagePayload) -> dict:
        """Fallback when the server itself cannot reach claude.ai: the app posts the raw /usage JSON."""
        if not collector.ingest(body.usage or {}):
            raise HTTPException(status_code=422, detail="no limits in payload")
        return current_payload()

    @app.get("/api/history")
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

    @app.get("/api/daily")
    def daily(key: str = "seven_day", days: int = Query(30, gt=0, le=400)) -> dict:
        samples = db.samples(key=key, since=time.time() - days * 86400)
        return {"key": key, "days": analytics.daily(samples, tz)}

    @app.get("/api/heatmap")
    def heatmap(key: str = "five_hour", days: int = Query(28, gt=0, le=400)) -> dict:
        samples = db.samples(key=key, since=time.time() - days * 86400)
        return {"key": key, "days": days, "cells": analytics.heatmap(samples, tz)}

    @app.get("/api/windows")
    def windows(key: str = "five_hour", days: int = Query(30, gt=0, le=400)) -> dict:
        samples = db.samples(key=key, since=time.time() - days * 86400)
        return {"key": key, "windows": list(reversed(analytics.windows(samples)))}

    @app.get("/api/summary")
    def summary(days: int = Query(30, gt=0, le=400)) -> dict:
        now = time.time()
        return analytics.summary(by_key(now - days * 86400), tz, now, days)

    @app.post("/api/tokens")
    def add_tokens(body: TokenReport) -> dict:
        """Totals from Claude Code's local logs, sent by mac/claude_tokens.py."""
        source = db.resolve_source(body.source)
        rows = [tokens.TokenRow(source=source, **r.model_dump()) for r in body.rows]
        db.upsert_tokens(rows, time.time(), replace_from=body.replace_from)
        return {"ok": True, "rows": len(rows)}

    @app.get("/api/tokens")
    def get_tokens(
        days: int = Query(30, gt=0, le=400),
        hours: int | None = Query(None, gt=0, le=72),
    ) -> dict:
        """Tokens per project for the last `hours` (15-minute resolution, bars per hour)
        or the last `days` (bars per day). `hours` wins when both are given."""
        now = time.time()
        # Rows with an impossible date (stored before dates were validated) are skipped
        # instead of breaking the whole answer.
        if hours:
            since_slot = int(now - hours * 3600) // tokens.SLOT * tokens.SLOT
            rows = [r for r in db.token_rows(since_slot, since_date=None) if parse_day(r.date)]
            meta = {"hours": hours, "days": None, "since": datetime.fromtimestamp(since_slot, tz).isoformat()}
            bucket = lambda r: r.slot_start // 3600 * 3600
        else:
            start = datetime.combine(datetime.now(tz).date() - timedelta(days=days - 1), dt_time(), tz)
            since = start.date().isoformat()
            # Slots are filtered by the Mac's own date so they line up with old per-day rows.
            day_starts: dict[str, int] = {}
            rows = []
            for r in db.token_rows(int(start.timestamp()) - 86400, since):
                if r.date < since:
                    continue
                if r.date not in day_starts:
                    day = parse_day(r.date)
                    if day is None:
                        continue
                    day_starts[r.date] = int(day.replace(tzinfo=tz).timestamp())
                rows.append(r)
            meta = {"hours": None, "days": days, "since": since}
            bucket = lambda r: day_starts[r.date]

        out = tokens.report(rows, bucket, **meta)
        sources = db.token_sources()
        out["sources"] = sources
        out["updated_at"] = max((s["updated_at"] for s in sources), default=None)
        return out

    @app.post("/api/devices")
    def add_device(body: Device) -> dict:
        db.add_device(body.token, time.time())
        return {"ok": True}

    @app.delete("/api/devices/{token}")
    def remove_device(token: str) -> dict:
        db.remove_device(token)
        return {"ok": True}

    @app.get("/api/export.csv")
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

    @app.get("/static/chart.umd.min.js", include_in_schema=False)
    def chart_js() -> FileResponse:
        """Chart.js for the dashboard, served here instead of a CDN (CSP: own scripts only).
        Public like /: it holds no data."""
        return FileResponse(STATIC / "chart.umd.min.js", media_type="text/javascript",
                            headers={"Cache-Control": "public, max-age=86400"})

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
