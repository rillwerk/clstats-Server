"""Runtime configuration, read once from environment variables."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo

from cryptography.fernet import Fernet

log = logging.getLogger(__name__)


def _env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    return value if value not in (None, "") else default


@dataclass(frozen=True)
class Settings:
    # Bearer token the iOS app and widgets send. Required.
    api_token: str
    # Fernet key used to encrypt the claude.ai cookie at rest (generated on first start if unset).
    secret_key: str
    # Optional second token that may only send token counts (POST /api/tokens), for the token
    # script on other machines. A leaked script config then cannot read data or change the login.
    ingest_token: str | None = None
    data_dir: Path = Path("/data")
    poll_interval: int = 300
    timezone: ZoneInfo = field(default_factory=lambda: ZoneInfo("UTC"))
    alert_thresholds: tuple[int, ...] = (50, 75, 90, 100)

    # Optional APNs push. All four must be set to enable it. Push to the App Store app needs the
    # app developer's APNs key, so self-hosted servers normally run without push.
    apns_key_path: Path | None = None
    apns_key_id: str | None = None
    apns_team_id: str | None = None
    apns_topic: str | None = None
    apns_sandbox: bool = True

    @property
    def db_path(self) -> Path:
        return self.data_dir / "usage.sqlite3"

    @property
    def apns_enabled(self) -> bool:
        return all((self.apns_key_path, self.apns_key_id, self.apns_team_id, self.apns_topic))


MIN_TOKEN_LENGTH = 24


def _secret_key(data_dir: Path) -> str:
    """SECRET_KEY from the environment, or one generated on first start and kept in the data volume.

    The key encrypts the stored claude.ai cookie. Losing it only means signing in again from the app.
    """
    key = _env("SECRET_KEY")
    path = data_dir / "secret.key"
    if not key and path.exists():
        key = path.read_text().strip()
    if not key:
        key = Fernet.generate_key().decode()
        data_dir.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(key + "\n")
        log.info("Generated a new SECRET_KEY in %s", path)
    try:
        Fernet(key.encode())
    except (ValueError, TypeError) as e:
        raise RuntimeError("SECRET_KEY is not a valid Fernet key (32 url-safe base64-encoded bytes)") from e
    return key


def load_settings() -> Settings:
    api_token = _env("API_TOKEN")
    if not api_token or len(api_token) < MIN_TOKEN_LENGTH:
        raise RuntimeError(
            f"API_TOKEN must be set to a random string of at least {MIN_TOKEN_LENGTH} characters "
            "(e.g. `openssl rand -hex 32`, see .env.example)"
        )
    ingest_token = _env("INGEST_TOKEN")
    if ingest_token is not None:
        if len(ingest_token) < MIN_TOKEN_LENGTH:
            raise RuntimeError(f"INGEST_TOKEN must be at least {MIN_TOKEN_LENGTH} characters (or left empty)")
        if ingest_token == api_token:
            raise RuntimeError("INGEST_TOKEN must differ from API_TOKEN")
    data_dir = Path(_env("DATA_DIR", "/data"))
    thresholds = tuple(int(t) for t in _env("ALERT_THRESHOLDS", "50,75,90,100").split(","))
    key_path = _env("APNS_KEY_PATH")
    return Settings(
        api_token=api_token,
        secret_key=_secret_key(data_dir),
        ingest_token=ingest_token,
        data_dir=data_dir,
        poll_interval=max(60, int(_env("POLL_INTERVAL", "300"))),
        timezone=ZoneInfo(_env("TZ", "UTC")),
        alert_thresholds=thresholds,
        apns_key_path=Path(key_path) if key_path else None,
        apns_key_id=_env("APNS_KEY_ID"),
        apns_team_id=_env("APNS_TEAM_ID"),
        apns_topic=_env("APNS_TOPIC"),
        apns_sandbox=_env("APNS_SANDBOX", "true").lower() == "true",
    )
