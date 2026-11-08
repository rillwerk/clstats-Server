"""Minimal APNs sender using token-based (.p8) authentication."""

from __future__ import annotations

import logging
import time

import httpx
import jwt

from .config import Settings

log = logging.getLogger(__name__)


class APNs:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._key = settings.apns_key_path.read_text() if settings.apns_enabled else None
        self._jwt: tuple[str, float] | None = None
        host = "api.sandbox.push.apple.com" if settings.apns_sandbox else "api.push.apple.com"
        self._base = f"https://{host}/3/device/"

    @property
    def enabled(self) -> bool:
        return self._key is not None

    def _token(self) -> str:
        # Apple accepts a provider token for up to an hour; refresh after 50 minutes.
        if self._jwt and time.time() - self._jwt[1] < 50 * 60:
            return self._jwt[0]
        now = time.time()
        token = jwt.encode(
            {"iss": self.settings.apns_team_id, "iat": int(now)},
            self._key,
            algorithm="ES256",
            headers={"kid": self.settings.apns_key_id},
        )
        self._jwt = (token, now)
        return token

    def send(self, device_tokens: list[str], title: str, body: str) -> list[str]:
        """Send an alert; returns tokens Apple reported as no longer valid."""
        if not self.enabled or not device_tokens:
            return []
        payload = {"aps": {"alert": {"title": title, "body": body}, "sound": "default"}}
        headers = {
            "authorization": f"bearer {self._token()}",
            "apns-topic": self.settings.apns_topic,
            "apns-push-type": "alert",
        }
        invalid: list[str] = []
        with httpx.Client(http2=True, timeout=15) as client:
            for token in device_tokens:
                try:
                    r = client.post(self._base + token, json=payload, headers=headers)
                except httpx.HTTPError as e:
                    log.warning("APNs request failed: %s", e)
                    continue
                if r.status_code == 410 or (r.status_code == 400 and "BadDeviceToken" in r.text):
                    invalid.append(token)
                elif r.status_code != 200:
                    log.warning("APNs answered %s: %s", r.status_code, r.text)
        return invalid
