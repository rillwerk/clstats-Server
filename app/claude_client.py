"""Client for the (unofficial) claude.ai usage endpoint.

Mirrors what ClaudeUsageBar does: authenticate with the browser session cookie,
find the organization id, then GET /api/organizations/{org}/usage.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime

import httpx

BASE = "https://claude.ai"
ORG_ID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
USER_AGENT = "ClaudeUsageApp/1.0 (personal usage monitor)"

LABELS = {
    "five_hour": "Session (5 h)",
    "seven_day": "Woche",
    "seven_day_sonnet": "Woche Sonnet",
    "seven_day_opus": "Woche Opus",
}


class ClaudeError(Exception):
    pass


class SessionExpired(ClaudeError):
    """Cookie rejected; the user has to log in again."""


class Blocked(ClaudeError):
    """Bot protection answered instead of the API."""


class RateLimited(ClaudeError):
    """claude.ai answered 429; retry_after is the wait it asked for in seconds, if any."""

    def __init__(self, message: str, retry_after: float | None = None):
        super().__init__(message)
        self.retry_after = retry_after


def retry_after_seconds(value: str | None) -> float | None:
    """Retry-After as seconds; accepts the delta-seconds and the HTTP-date form."""
    if not value:
        return None
    value = value.strip()
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        from email.utils import parsedate_to_datetime

        return max(0.0, parsedate_to_datetime(value).timestamp() - datetime.now().timestamp())
    except (TypeError, ValueError):
        return None


@dataclass(frozen=True)
class Reading:
    key: str
    label: str
    utilization: float
    resets_at: float | None


def parse_time(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def parse_usage(payload: dict) -> list[Reading]:
    """Turn the /usage JSON into readings, tolerating unknown and missing fields."""
    readings: list[Reading] = []
    for key, value in payload.items():
        if not isinstance(value, dict) or "utilization" not in value:
            continue
        util = value.get("utilization")
        if not isinstance(util, (int, float)):
            continue
        label = LABELS.get(key, key.replace("_", " ").capitalize())
        readings.append(Reading(key, label, float(util), parse_time(value.get("resets_at"))))

    for entry in payload.get("limits") or []:
        if not isinstance(entry, dict):
            continue
        model = ((entry.get("scope") or {}).get("model") or {}).get("display_name")
        percent = entry.get("percent")
        if not model or not isinstance(percent, (int, float)):
            continue
        readings.append(
            Reading(f"model:{model}", f"Woche {model}", float(percent), parse_time(entry.get("resets_at")))
        )
    return readings


def normalize_cookie(raw: str) -> str:
    """Accept either a bare sessionKey value or a full Cookie header."""
    raw = raw.strip()
    if raw.lower().startswith("cookie:"):
        raw = raw[7:].strip()
    if "=" not in raw:
        return f"sessionKey={raw}"
    return raw


def cookie_value(cookie: str, name: str) -> str | None:
    for part in cookie.split(";"):
        k, _, v = part.strip().partition("=")
        if k == name and v:
            return v
    return None


class ClaudeClient:
    def __init__(self, cookie: str, timeout: float = 20):
        self.cookie = normalize_cookie(cookie)
        self.timeout = timeout
        self.session = httpx.Client(headers={"User-Agent": USER_AGENT}, follow_redirects=False)

    def _get(self, path: str) -> dict:
        resp = self.session.get(
            BASE + path,
            headers={
                "Cookie": self.cookie,
                "Accept": "application/json",
                "Referer": f"{BASE}/settings/usage",
                "Origin": BASE,
            },
            timeout=self.timeout,
        )
        ctype = resp.headers.get("content-type", "")
        if resp.status_code == 429:
            # Checked before the HTML test: a 429 page from claude.ai's edge is a rate limit,
            # not bot protection, and must not send every app to claude.ai as a fallback.
            raise RateLimited("claude.ai answered 429", retry_after_seconds(resp.headers.get("retry-after")))
        if resp.status_code in (401, 403) and "json" in ctype:
            raise SessionExpired(f"claude.ai answered {resp.status_code}")
        if resp.status_code == 403 or "text/html" in ctype:
            raise Blocked(f"claude.ai answered {resp.status_code} with {ctype or 'no content type'}")
        if resp.status_code >= 400:
            raise ClaudeError(f"claude.ai answered {resp.status_code}")
        return resp.json()

    def organization_id(self) -> str:
        org = cookie_value(self.cookie, "lastActiveOrg")
        if org:
            return org
        data = self._get("/api/bootstrap")
        if not data.get("account"):
            raise SessionExpired("claude.ai does not recognise this session")
        org = (data.get("account") or {}).get("lastActiveOrgId")
        if not org:
            # Prefer the organization with the chat subscription; an account can also
            # belong to API-only organizations, whose /usage answers 403.
            orgs = [m.get("organization") or {} for m in (data.get("account") or {}).get("memberships") or []]
            orgs.sort(key=lambda o: "chat" not in (o.get("capabilities") or []))
            org = next((o.get("uuid") for o in orgs if o.get("uuid")), None)
        if not org:
            raise ClaudeError("no organization id found")
        return org

    def usage(self, org_id: str) -> dict:
        # The id goes into the request path; only accept the UUID form claude.ai uses.
        if not ORG_ID.match(org_id):
            raise ClaudeError("organization id has an unexpected format")
        return self._get(f"/api/organizations/{org_id}/usage")
