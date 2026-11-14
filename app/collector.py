"""Polls claude.ai on a fixed interval, stores samples and sends alerts."""

from __future__ import annotations

import json
import logging
import threading
import time
from collections import defaultdict
from typing import Callable

import httpx
from cryptography.fernet import Fernet, InvalidToken

from . import analytics
from .alerts import alerts_for
from .apns import APNs
from .claude_client import Blocked, ClaudeClient, ClaudeError, RateLimited, SessionExpired, parse_usage
from .config import Settings
from .db import Database, Sample

log = logging.getLogger(__name__)


class Collector:
    def __init__(
        self,
        settings: Settings,
        db: Database,
        push: APNs | None = None,
        client_factory: Callable[[str], ClaudeClient] = ClaudeClient,
    ):
        self.settings = settings
        self.db = db
        self.push = push
        self.client_factory = client_factory
        self._fernet = Fernet(settings.secret_key.encode())
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        # After a 429 from claude.ai: no requests before _pause_until, the wait doubles per 429.
        self._pause_until = 0.0
        self._backoff = 0.0
        # One polling round at a time (collector thread, /api/refresh, /api/credentials).
        # A caller that finds a round running waits for it and takes its result; it only
        # polls again if the login changed meanwhile (_generation counts set_cookie calls).
        self._run_lock = threading.Lock()
        self._generation = 0
        self._last_run: tuple[int, list[Sample]] | None = None

    # credentials

    def set_cookie(self, cookie: str, org_id: str | None = None) -> None:
        self.db.set("cookie", self._fernet.encrypt(cookie.encode()).decode())
        self.db.set("org_id", org_id)
        self.db.set("session_alerted", None)
        self._pause_until = self._backoff = 0.0
        self._generation += 1
        self._wake.set()

    def clear_cookie(self) -> None:
        self.db.set("cookie", None)
        self.db.set("org_id", None)

    def cookie(self) -> str | None:
        enc = self.db.get("cookie")
        if not enc:
            return None
        try:
            return self._fernet.decrypt(enc.encode()).decode()
        except InvalidToken:
            log.error("Stored cookie cannot be decrypted; was SECRET_KEY changed?")
            return None

    # status

    def status(self) -> dict:
        raw = self.db.get("status")
        status = json.loads(raw) if raw else {}
        status["configured"] = self.cookie() is not None
        status["poll_interval"] = self.settings.poll_interval
        status["push_enabled"] = bool(self.push and self.push.enabled)
        return status

    def _set_status(self, **fields) -> None:
        status = json.loads(self.db.get("status") or "{}")
        status.update(fields)
        self.db.set("status", json.dumps(status))

    # polling

    def run_once(self, now: float | None = None) -> list[Sample]:
        waited = not self._run_lock.acquire(blocking=False)
        if waited:
            self._run_lock.acquire()
        try:
            if waited and self._last_run and self._last_run[0] == self._generation:
                return self._last_run[1]
            generation = self._generation
            result = self._run_once(now)
            self._last_run = (generation, result)
            return result
        finally:
            self._run_lock.release()

    def _run_once(self, now: float | None) -> list[Sample]:
        now = now or time.time()
        cookie = self.cookie()
        if not cookie:
            self._set_status(last_attempt=now, error="not_configured", error_detail="Kein Login hinterlegt")
            return []
        if now < self._pause_until:
            return []  # still waiting out a rate limit; the status already says so
        client = self.client_factory(cookie)
        try:
            org = self.db.get("org_id") or client.organization_id()
            readings = parse_usage(client.usage(org))
            # Stored only once a fetch with it worked (usage() also checks the UUID form),
            # so a wrong id is looked up again next round instead of sticking.
            self.db.set("org_id", org)
        except SessionExpired as e:
            self._set_status(last_attempt=now, error="session_expired", error_detail=str(e))
            if self.db.get("session_alerted") is None:
                self.db.set("session_alerted", "1")
                self._notify("Claude-Login abgelaufen", "Bitte in der App neu bei claude.ai anmelden.")
            return []
        except RateLimited as e:
            self._backoff = min(3600.0, max(float(self.settings.poll_interval), self._backoff * 2))
            wait = max(e.retry_after or 0.0, self._backoff)
            self._pause_until = now + wait
            log.warning("claude.ai rate limit, next attempt in %d s", wait)
            self._set_status(last_attempt=now, error="rate_limited", error_detail=str(e), retry_at=self._pause_until)
            return []
        except Blocked as e:
            self._set_status(last_attempt=now, error="blocked", error_detail=str(e))
            return []
        except (ClaudeError, OSError, httpx.TimeoutException, httpx.NetworkError) as e:
            # Our own messages and network failures: they never contain request headers.
            self._set_status(last_attempt=now, error="fetch_failed", error_detail=str(e))
            return []
        except Exception as e:  # other httpx errors, JSON errors
            # Only the type: some httpx errors quote the offending header value, i.e. the
            # cookie ("Illegal header value b'...'"), and that must not reach the status,
            # the app, the dashboard or the log. Hence no traceback either.
            log.error("Fetch failed: %s", type(e).__name__)
            self._set_status(last_attempt=now, error="fetch_failed", error_detail=type(e).__name__)
            return []
        finally:
            close = getattr(client, "close", None)
            if close:
                close()

        self._backoff = 0.0
        return self.store(readings, now)

    def ingest(self, payload: dict, now: float | None = None) -> list[Sample]:
        """Store a /usage response fetched elsewhere, e.g. by the iPhone app itself."""
        return self.store(parse_usage(payload), now or time.time())

    def store(self, readings, now: float) -> list[Sample]:
        samples = [Sample(now, r.key, r.label, r.utilization, r.resets_at) for r in readings]
        if not samples:
            self._set_status(last_attempt=now, error="empty", error_detail="Keine Limits in der Antwort")
            return []
        self.db.add_samples(samples)
        self._set_status(last_attempt=now, last_success=now, error=None, error_detail=None, retry_at=None)
        self._check_alerts(now)
        return samples

    def forecasts(self, now: float) -> list[analytics.Forecast]:
        since = now - 8 * 24 * 3600
        by_key: dict[str, list[Sample]] = defaultdict(list)
        for s in self.db.samples(since=since):
            by_key[s.key].append(s)
        latest_keys = {s.key for s in self.db.latest()}
        out = []
        for key, samples in by_key.items():
            if key in latest_keys:
                f = analytics.forecast(samples, now)
                if f:
                    out.append(f)
        order = {"five_hour": 0, "seven_day": 1}
        return sorted(out, key=lambda f: (order.get(f.key, 2), f.key))

    def _check_alerts(self, now: float) -> None:
        for alert in alerts_for(self.forecasts(now), self.settings.alert_thresholds, self.settings.timezone):
            if self.db.mark_alert(alert.key, alert.window, alert.kind, now) and not alert.silent:
                self._notify(alert.title, alert.body)

    def _notify(self, title: str, body: str) -> None:
        log.info("Alert: %s – %s", title, body)
        if not self.push or not self.push.enabled:
            return
        for token in self.push.send(self.db.devices(), title, body):
            self.db.remove_device(token)

    # background thread

    def refresh_now(self) -> None:
        self._wake.set()

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, name="collector", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.run_once()
            except Exception:
                log.exception("Collector round failed")
            self._wake.wait(self.settings.poll_interval)
            self._wake.clear()
