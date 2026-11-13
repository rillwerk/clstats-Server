import time

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from app import analytics
from app.alerts import alerts_for
from app.claude_client import ClaudeClient, SessionExpired, normalize_cookie, parse_usage
from app.collector import Collector
from app.config import Settings
from app.db import Database, Sample
from app.main import create_app

USAGE = {
    "five_hour": {"utilization": 42.0, "resets_at": "2026-10-27T20:00:00.123+00:00"},
    "seven_day": {"utilization": 61, "resets_at": "2026-10-30T08:00:00Z"},
    "seven_day_opus": None,
    "iguana_necktie": {"limit_dollars": 0},
    "limits": [{"scope": {"model": {"display_name": "Fable"}}, "percent": 7, "resets_at": "2026-10-30T08:00:00Z"}],
}


def test_parse_usage_is_generic():
    readings = {r.key: r for r in parse_usage(USAGE)}
    assert set(readings) == {"five_hour", "seven_day", "model:Fable"}
    assert readings["five_hour"].utilization == 42.0
    assert readings["seven_day"].label == "Woche"
    assert readings["model:Fable"].resets_at is not None


def test_normalize_cookie():
    assert normalize_cookie("sk-ant-sid01-abc") == "sessionKey=sk-ant-sid01-abc"
    assert normalize_cookie("Cookie: a=1; sessionKey=x") == "a=1; sessionKey=x"


def s(ts, util, resets, key="five_hour"):
    return Sample(ts, key, "Session", util, resets)


def test_forecast_projects_limit_before_reset():
    now = 1_000_000.0
    reset = now + 2 * 3600
    samples = [s(now - 3600 + i * 600, 40 + i * 5, reset) for i in range(7)]  # 30 %/h, now at 70 %
    f = analytics.forecast(samples, now)
    assert f.rate_per_hour == pytest.approx(30, rel=1e-3)
    assert f.status == "limit_before_reset"
    assert f.limit_eta == pytest.approx(now + 3600, rel=1e-6)
    assert f.pace == pytest.approx(60)


def test_forecast_ok_when_flat():
    now = 1_000_000.0
    samples = [s(now - 1800 + i * 300, 20, now + 3600) for i in range(7)]
    f = analytics.forecast(samples, now)
    assert f.status == "ok" and f.limit_eta is None


def test_deltas_handle_resets_and_daily():
    samples = [s(0, 10, 18000), s(300, 30, 18000), s(600, 25, 18000), s(20000, 5, 36000), s(20300, 15, 36000)]
    assert [d for _, d in analytics.deltas(samples)] == [20, 5, 10]
    wins = analytics.windows(samples)
    assert len(wins) == 2 and wins[0]["peak"] == 30 and not wins[0]["early_reset"]


def test_early_reset_detected():
    samples = [s(0, 80, 18000), s(3600, 0, 3600 + 18000)]
    assert analytics.windows(samples)[0]["early_reset"] is True


def test_alerts_only_top_threshold_is_loud():
    f = analytics.Forecast("five_hour", "Session", 92, 18000, 50, 0, 92, None, "tight")
    from zoneinfo import ZoneInfo
    alerts = alerts_for([f], (50, 75, 90), ZoneInfo("Europe/Berlin"))
    assert [(a.kind, a.silent) for a in alerts] == [("threshold:50", True), ("threshold:75", True), ("threshold:90", False)]


class FakeClient:
    payload = USAGE
    fail = None

    def __init__(self, cookie):
        self.cookie = cookie

    def organization_id(self):
        return "org-1"

    def usage(self, org):
        if FakeClient.fail:
            raise FakeClient.fail
        return FakeClient.payload


@pytest.fixture
def setup(tmp_path):
    settings = Settings(api_token="t0ken", secret_key=Fernet.generate_key().decode(), data_dir=tmp_path)
    collector = Collector(settings, Database(settings.db_path), None, FakeClient)
    FakeClient.fail = None
    app = create_app(settings, collector)
    client = TestClient(app)
    client.headers["Authorization"] = "Bearer t0ken"
    return client, collector


def test_api_flow(setup):
    client, collector = setup
    assert TestClient(client.app).get("/api/current").status_code == 401
    r = client.get("/api/current").json()
    assert r["status"]["configured"] is False
    r = client.post("/api/credentials", json={"cookie": "sessionKey=sk-ant-test"}).json()
    assert r["status"]["error"] is None
    assert [l["key"] for l in r["limits"]] == ["five_hour", "seven_day", "model:Fable"]
    assert collector.db.get("cookie") != "sessionKey=sk-ant-test"  # stored encrypted
    assert client.get("/api/history", params={"key": "five_hour"}).json()["points"]
    assert "five_hour" in client.get("/api/export.csv").text
    assert client.get("/api/summary").status_code == 200
    assert client.get("/api/export.csv", params={"access_token": "t0ken"}, headers={"Authorization": ""}).status_code == 200


def test_privacy_page_is_public(setup):
    client, _ = setup
    r = TestClient(client.app).get("/privacy")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    assert "self-hosted" in r.text and "Verantwortlicher" not in r.text


def test_session_expired_status(setup):
    client, collector = setup
    collector.set_cookie("sessionKey=x" * 2)
    FakeClient.fail = SessionExpired("401")
    collector.run_once()
    assert client.get("/api/current").json()["status"]["error"] == "session_expired"


def test_ingest_from_app(setup):
    client, _ = setup
    r = client.post("/api/ingest", json={"usage": USAGE}).json()
    assert r["limits"][0]["utilization"] == 42.0
    assert client.post("/api/ingest", json={"usage": {}}).status_code == 422


def test_organization_prefers_chat_org(monkeypatch):
    bootstrap = {"account": {"lastActiveOrgId": None, "memberships": [
        {"organization": {"uuid": "api-org", "capabilities": ["api", "api_individual"]}},
        {"organization": {"uuid": "chat-org", "capabilities": ["claude_max", "chat"]}},
    ]}}
    client = ClaudeClient("sessionKey=x")
    monkeypatch.setattr(client, "_get", lambda path: bootstrap)
    assert client.organization_id() == "chat-org"


def test_token_prices_and_names():
    from app import tokens

    assert tokens.price_for("claude-opus-4-8") == (5.0, 25.0, 0.5)
    assert tokens.price_for("claude-opus-4-1-20250805") == (15.0, 75.0, 1.5)
    assert tokens.price_for("claude-sonnet-4-5-20250929")[0] == 3.0
    assert tokens.price_for("gpt-irgendwas") is None
    assert tokens.pretty_model("claude-opus-4-8") == "Opus 4.8"
    assert tokens.pretty_model("claude-sonnet-4-5-20250929") == "Sonnet 4.5"
    assert tokens.pretty_model("claude-3-5-haiku-20241022") == "Haiku 3.5"
    assert tokens.pretty_model("claude-fable-5") == "Fable 5"
    # 1M input + 1M output + 1M 5-min cache write + 1M cache read on Opus 4.8
    t = {"input": 10**6, "output": 10**6, "cache_write": 10**6, "cache_write_1h": 0, "cache_read": 10**6}
    assert tokens.cost("claude-opus-4-8", t) == pytest.approx(5 + 25 + 6.25 + 0.5)


def test_tokens_api(setup):
    from datetime import date, timedelta

    client, _ = setup
    today = date.today()
    old = (today - timedelta(days=40)).isoformat()
    row = lambda d, p, m, n: {"date": d, "project": p, "model": m, "input": n, "output": n, "cache_read": 10 * n, "messages": 1}
    rows = [
        row(today.isoformat(), "ClaudeUsageApp", "claude-opus-4-8", 1000),
        row(today.isoformat(), "ClaudeUsageApp", "claude-haiku-4-5", 500),
        row(today.isoformat(), "KochHerz", "claude-sonnet-4-5", 100),
        row(old, "KochHerz", "claude-sonnet-4-5", 9999),
    ]
    assert client.post("/api/tokens", json={"source": "mac", "rows": rows}).json() == {"ok": True, "rows": 4}
    # A later report with fewer tokens (logs cleaned up) must not shrink stored totals.
    client.post("/api/tokens", json={"source": "mac", "rows": [row(today.isoformat(), "KochHerz", "claude-sonnet-4-5", 1)]})

    r = client.get("/api/tokens", params={"days": 30}).json()
    assert [p["project"] for p in r["projects"]] == ["ClaudeUsageApp", "KochHerz"]
    app = r["projects"][0]
    assert app["total"] == 1000 * 12 + 500 * 12
    assert [m["name"] for m in app["models"]] == ["Opus 4.8", "Haiku 4.5"]
    assert app["daily"][0]["date"] == today.isoformat()
    assert r["projects"][1]["input"] == 100
    assert r["total"]["messages"] == 3
    assert r["total"]["cost"] > 0
    assert r["sources"][0]["source"] == "mac"
    assert client.get("/api/tokens", params={"days": 60}).json()["total"]["input"] == 1000 + 500 + 100 + 9999
    assert client.post("/api/tokens", json={"source": "mac", "rows": [{**rows[0], "date": "heute"}]}).status_code == 422


def test_tokens_by_slot_and_hours(setup):
    from datetime import date, datetime

    client, _ = setup
    now = time.time()
    slot = lambda ago: int(now - ago) // 900 * 900
    day = lambda ts: datetime.fromtimestamp(ts).date().isoformat()
    row = lambda ts, p, n: {"date": day(ts), "slot_start": ts, "project": p, "model": "claude-opus-4-8",
                            "input": n, "output": 0, "messages": 1}
    today = date.today().isoformat()
    # An old script reported today as one day row; the new one sends slots for the same day.
    client.post("/api/tokens", json={"source": "mac", "rows": [
        {"date": today, "project": "A", "model": "claude-opus-4-8", "input": 999, "messages": 9}]})
    rows = [row(slot(10 * 60), "A", 10), row(slot(3 * 3600), "A", 20), row(slot(20 * 3600), "B", 40)]
    client.post("/api/tokens", json={"source": "mac", "rows": rows})

    r = client.get("/api/tokens", params={"hours": 1}).json()
    assert r["hours"] == 1 and r["total"]["input"] == 10
    r = client.get("/api/tokens", params={"hours": 6}).json()
    assert r["total"]["input"] == 30
    assert all(p["start"] % 3600 == 0 for p in r["series"])
    r = client.get("/api/tokens", params={"hours": 24}).json()
    assert {p["project"]: p["input"] for p in r["projects"]} == {"A": 30, "B": 40}
    # Days view: the day row was replaced by the slots, nothing is counted twice.
    r = client.get("/api/tokens", params={"days": 7}).json()
    assert r["total"]["input"] == 70 and r["days"] == 7 and r["hours"] is None
    assert sum(p["input"] for p in r["series"]) == 70


def test_tokens_replace_from_drops_renamed_rows(setup):
    """A newer script renames a project: with replace_from the old rows go away
    instead of being counted next to the new ones."""
    client, _ = setup
    now = time.time()
    s1 = int(now - 3600) // 900 * 900
    old = int(now - 30 * 86400) // 900 * 900
    from datetime import datetime
    row = lambda ts, p, n: {"date": datetime.fromtimestamp(ts).date().isoformat(), "slot_start": ts,
                            "project": p, "model": "claude-opus-4-8", "input": n, "messages": 1}
    client.post("/api/tokens", json={"source": "mac", "rows": [row(old, "Alt", 5), row(s1, ".secrets", 100)]})
    client.post("/api/tokens", json={"source": "other", "rows": [row(s1, "X", 7)]})
    client.post("/api/tokens", json={"source": "mac", "rows": [row(s1, "Projekt", 100)], "replace_from": s1})
    r = client.get("/api/tokens", params={"days": 60}).json()
    assert {p["project"]: p["input"] for p in r["projects"]} == {"Alt": 5, "Projekt": 100, "X": 7}


def test_merge_token_sources(setup):
    """One MacBook reported under several DHCP host names: merge without double counting."""
    client, collector = setup
    db = collector.db
    from datetime import datetime
    s1 = int(time.time() - 3600) // 900 * 900
    row = lambda p, n: {"date": datetime.fromtimestamp(s1).date().isoformat(), "slot_start": s1,
                        "project": p, "model": "claude-opus-4-8", "input": n, "messages": 1}
    client.post("/api/tokens", json={"source": "WML-A", "rows": [row("hausgeist", 100)]})
    client.post("/api/tokens", json={"source": "Mac", "rows": [row("hausgeist", 120), row("lea", 5)]})
    db.merge_token_sources(["WML-A", "Mac"], "MacBook")
    client.post("/api/tokens", json={"source": "Mac", "rows": [row("hausgeist", 130)]})
    r = client.get("/api/tokens", params={"days": 7}).json()
    assert {p["project"]: p["input"] for p in r["projects"]} == {"hausgeist": 130, "lea": 5}
    assert [x["source"] for x in db.token_sources()] == ["MacBook"]


def test_db_parallel_reads_and_writes(tmp_path):
    """The app shares one connection across threads; parallel requests used to fail with
    'bad parameter or other API misuse' or half-read rows (HTTP 500)."""
    from concurrent.futures import ThreadPoolExecutor

    db = Database(tmp_path / "t.db")
    now = time.time()
    db.add_samples([s(now - i, i % 100, None) for i in range(500)])

    def work(i):
        if i % 4 == 0:
            db.add_samples([s(now + i, 1, None)])
        for _ in range(20):
            assert all(isinstance(x, Sample) for x in db.samples(key="five_hour"))
            db.latest()
            db.token_rows()
            db.get("x")

    with ThreadPoolExecutor(max_workers=16) as pool:
        list(pool.map(work, range(64)))


def test_failed_tokens_are_throttled(setup):
    client, _ = setup
    anon = TestClient(client.app)
    for _ in range(10):
        assert anon.get("/api/current", headers={"Authorization": "Bearer wrong"}).status_code == 401
    # Further wrong tokens are blocked and told how long to wait ...
    r = anon.get("/api/current", headers={"Authorization": "Bearer wrong"})
    assert r.status_code == 429
    assert 1 <= int(r.headers["Retry-After"]) <= 601
    # ... but the right token still works, so a scanner cannot lock out the owner
    assert anon.get("/api/current", headers={"Authorization": "Bearer t0ken"}).status_code == 200
    assert anon.get("/api/health").status_code == 200


def test_auth_failures_window():
    from app.main import AuthFailures
    f = AuthFailures(per_client=2, overall=3, window=60)
    f.add("a", now=0); f.add("a", now=1)
    assert f.blocked("a", now=2) and not f.blocked("b", now=2)
    assert f.retry_after("a", now=2) == 59  # the oldest failure (t=0) leaves the window at t=60
    f.add("b", now=3)
    assert f.blocked("c", now=4)  # global cap
    assert not f.blocked("a", now=100)


def test_claude_rate_limit_backs_off(setup):
    from app.claude_client import RateLimited
    client, collector = setup
    client.post("/api/credentials", json={"cookie": "sessionKey=sk-ant-test"})
    FakeClient.fail = RateLimited("claude.ai answered 429", retry_after=900)
    now = time.time()
    assert collector.run_once(now) == []
    status = collector.status()
    assert status["error"] == "rate_limited" and status["retry_at"] == pytest.approx(now + 900)
    # No request to claude.ai while waiting, even if it would succeed now
    FakeClient.fail = None
    assert collector.run_once(now + 60) == []
    assert collector.status()["error"] == "rate_limited"
    assert collector.run_once(now + 901)
    assert collector.status()["error"] is None and collector.status()["retry_at"] is None


def test_claude_429_is_rate_limit_not_blocked():
    import httpx
    from app.claude_client import RateLimited

    def handler(request):
        return httpx.Response(429, headers={"content-type": "text/html", "retry-after": "120"}, text="<html>")

    c = ClaudeClient("sessionKey=x; lastActiveOrg=o")
    c.session = httpx.Client(transport=httpx.MockTransport(handler))
    with pytest.raises(RateLimited) as e:
        c.usage("o")
    assert e.value.retry_after == 120


def test_load_settings_requires_long_token_and_generates_secret(tmp_path, monkeypatch):
    from app.config import load_settings
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.delenv("SECRET_KEY", raising=False)
    monkeypatch.setenv("API_TOKEN", "short")
    with pytest.raises(RuntimeError):
        load_settings()
    monkeypatch.setenv("API_TOKEN", "x" * 32)
    first = load_settings()
    assert (tmp_path / "secret.key").exists()
    assert oct((tmp_path / "secret.key").stat().st_mode & 0o777) == "0o600"
    Fernet(first.secret_key.encode())
    assert load_settings().secret_key == first.secret_key  # kept across restarts
    monkeypatch.setenv("SECRET_KEY", "not-a-key")
    with pytest.raises(RuntimeError):
        load_settings()


def test_tokens_by_model_detail(setup):
    from datetime import datetime

    from app import tokens

    client, _ = setup
    slot = int(datetime.now().timestamp()) // 900 * 900 - 900
    day = datetime.fromtimestamp(slot).date().isoformat()
    row = lambda p, m, n, s=slot: {
        "slot_start": s, "date": day, "project": p, "model": m, "input": n, "output": n,
        "cache_write": 2 * n, "cache_write_1h": n, "cache_read": 10 * n, "messages": 1,
    }
    rows = [
        row("alpha", "claude-fable-5-1", 1000),
        row("beta", "claude-fable-5-1", 3000, slot - 3600),
        row("alpha", "claude-opus-5-5", 500),
    ]
    client.post("/api/tokens", json={"source": "mac", "rows": rows})
    r = client.get("/api/tokens", params={"hours": 24}).json()
    # Existing fields stay as they were for older app builds.
    assert {"total", "projects", "models", "daily", "series", "sources"} <= r.keys()
    assert [m["model"] for m in r["models"]] == [m["model"] for m in r["by_model"]]
    fable = r["by_model"][0]
    assert fable["name"] == "Fable 5.1"
    assert fable["total"] == 4000 * 14
    assert [p["project"] for p in fable["projects"]] == ["beta", "alpha"]
    assert fable["projects"][0]["total"] == 3000 * 14
    assert len(fable["series"]) == 2 and sum(b["total"] for b in fable["series"]) == fable["total"]
    c = fable["costs"]
    assert c["input"] == pytest.approx(4000 * 10 / 1e6)
    assert c["output"] == pytest.approx(4000 * 50 / 1e6)
    # 4000 written for 5 minutes (1.25x) and 4000 for one hour (2x)
    assert c["cache_write"] == pytest.approx(4000 * 10 * (1.25 + 2.0) / 1e6)
    assert c["cache_read"] == pytest.approx(40000 * 0.25 / 1e6)
    assert sum(c.values()) == pytest.approx(fable["cost"], abs=1e-3)
    assert tokens.cost("claude-fable-5-1", fable) == pytest.approx(fable["cost"], abs=1e-3)
