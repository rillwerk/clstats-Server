"""SQLite storage for samples, settings and alert state."""

from __future__ import annotations

import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path

from .tokens import TokenRow

# replace_from never reaches further back than this: the oldest data the dashboard shows.
MAX_REPLACE_AGE = 400 * 86400

SCHEMA = """
CREATE TABLE IF NOT EXISTS kv (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS samples (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          REAL NOT NULL,          -- unix seconds, fetch time
    key         TEXT NOT NULL,          -- five_hour, seven_day, seven_day_sonnet, model:<name>
    label       TEXT NOT NULL,
    utilization REAL NOT NULL,          -- percent, 0..100+
    resets_at   REAL                    -- unix seconds or NULL when no window is running
);
CREATE INDEX IF NOT EXISTS samples_key_ts ON samples (key, ts);
CREATE TABLE IF NOT EXISTS devices (
    token   TEXT PRIMARY KEY,
    created REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS alerts_sent (
    key    TEXT NOT NULL,
    window TEXT NOT NULL,
    kind   TEXT NOT NULL,
    ts     REAL NOT NULL,
    PRIMARY KEY (key, window, kind)
);
CREATE TABLE IF NOT EXISTS token_usage (
    source         TEXT NOT NULL,       -- Mac host name that reported the row
    date           TEXT NOT NULL,       -- YYYY-MM-DD, local time of that Mac
    project        TEXT NOT NULL,
    model          TEXT NOT NULL,
    input          INTEGER NOT NULL,
    output         INTEGER NOT NULL,
    cache_write    INTEGER NOT NULL,
    cache_write_1h INTEGER NOT NULL,
    cache_read     INTEGER NOT NULL,
    messages       INTEGER NOT NULL,
    updated        REAL NOT NULL,
    PRIMARY KEY (source, date, project, model)
);
CREATE INDEX IF NOT EXISTS token_usage_date ON token_usage (date);
CREATE TABLE IF NOT EXISTS token_slots (
    source         TEXT NOT NULL,
    slot_start     INTEGER NOT NULL,    -- unix seconds, 15-minute slot
    date           TEXT NOT NULL,       -- YYYY-MM-DD of the slot, local time of that Mac
    project        TEXT NOT NULL,
    model          TEXT NOT NULL,
    input          INTEGER NOT NULL,
    output         INTEGER NOT NULL,
    cache_write    INTEGER NOT NULL,
    cache_write_1h INTEGER NOT NULL,
    cache_read     INTEGER NOT NULL,
    messages       INTEGER NOT NULL,
    updated        REAL NOT NULL,
    PRIMARY KEY (source, slot_start, project, model)
);
CREATE INDEX IF NOT EXISTS token_slots_start ON token_slots (slot_start);
CREATE TABLE IF NOT EXISTS token_source_alias (
    alias  TEXT PRIMARY KEY,            -- name a machine reported under (e.g. a DHCP host name)
    source TEXT NOT NULL                -- the name its rows are stored under
);
"""


@dataclass(frozen=True)
class Sample:
    ts: float
    key: str
    label: str
    utilization: float
    resets_at: float | None


class Database:
    def __init__(self, path: Path | str):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(SCHEMA)
        # One connection is shared by FastAPI's worker threads and the collector.
        # sqlite3 objects must not be used by two threads at once, so reads take the lock too.
        self._lock = threading.RLock()

    # kv

    def get(self, key: str) -> str | None:
        with self._lock:
            row = self._conn.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None

    def set(self, key: str, value: str | None) -> None:
        with self._lock, self._conn:
            if value is None:
                self._conn.execute("DELETE FROM kv WHERE key = ?", (key,))
            else:
                self._conn.execute(
                    "INSERT INTO kv (key, value) VALUES (?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    (key, value),
                )

    # samples

    def add_samples(self, samples: list[Sample]) -> None:
        with self._lock, self._conn:
            self._conn.executemany(
                "INSERT INTO samples (ts, key, label, utilization, resets_at) VALUES (?, ?, ?, ?, ?)",
                [(s.ts, s.key, s.label, s.utilization, s.resets_at) for s in samples],
            )

    def samples(self, key: str | None = None, since: float = 0, until: float | None = None) -> list[Sample]:
        sql = "SELECT ts, key, label, utilization, resets_at FROM samples WHERE ts >= ?"
        args: list = [since]
        if until is not None:
            sql += " AND ts <= ?"
            args.append(until)
        if key is not None:
            sql += " AND key = ?"
            args.append(key)
        sql += " ORDER BY ts"
        with self._lock:
            return [Sample(*row) for row in self._conn.execute(sql, args)]

    def latest(self) -> list[Sample]:
        """Most recent sample of every key seen in the last fetch round."""
        with self._lock:
            row = self._conn.execute("SELECT MAX(ts) FROM samples").fetchone()
            if not row or row[0] is None:
                return []
            return self.samples(since=row[0], until=row[0])

    # devices

    def add_device(self, token: str, now: float) -> None:
        with self._lock, self._conn:
            self._conn.execute("INSERT OR IGNORE INTO devices (token, created) VALUES (?, ?)", (token, now))

    def remove_device(self, token: str) -> None:
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM devices WHERE token = ?", (token,))

    def devices(self) -> list[str]:
        with self._lock:
            return [r[0] for r in self._conn.execute("SELECT token FROM devices")]

    # alerts

    def mark_alert(self, key: str, window: str, kind: str, now: float) -> bool:
        """Record an alert; returns False when it was already sent for this window."""
        with self._lock, self._conn:
            cur = self._conn.execute(
                "INSERT OR IGNORE INTO alerts_sent (key, window, kind, ts) VALUES (?, ?, ?, ?)",
                (key, window, kind, now),
            )
            return cur.rowcount == 1

    # tokens

    _COUNTS = "input, output, cache_write, cache_write_1h, cache_read, messages"
    _MAX = (
        "input = MAX(input, excluded.input), output = MAX(output, excluded.output), "
        "cache_write = MAX(cache_write, excluded.cache_write), "
        "cache_write_1h = MAX(cache_write_1h, excluded.cache_write_1h), "
        "cache_read = MAX(cache_read, excluded.cache_read), "
        "messages = MAX(messages, excluded.messages), updated = excluded.updated"
    )

    def upsert_tokens(self, rows: list[TokenRow], now: float, replace_from: int | None = None) -> None:
        """Store reported totals. Counts only ever grow: Claude Code deletes old logs,
        so a later report can be missing part of a period the server already has in full.

        Rows with a slot_start go to token_slots (15-minute resolution). Older scripts send
        per-day rows; once a source reports slots, its per-day rows from the first reported
        day on are dropped so nothing is counted twice.

        With replace_from the report is complete from that slot on: the reporting source's
        slot rows there are replaced instead of merged, so rows under an old project name
        do not linger next to the new ones. It never reaches below the oldest slot the report
        itself contains (and does nothing for a report without slots) nor more than 400 days
        back: a report can only replace what it brings along, so replace_from=0 cannot wipe
        a source's history."""
        slots = [r for r in rows if r.slot_start is not None]
        days = [r for r in rows if r.slot_start is None]
        counts = lambda r: (r.input, r.output, r.cache_write, r.cache_write_1h, r.cache_read, r.messages)
        with self._lock, self._conn:
            if replace_from is not None:
                for source in {r.source for r in slots}:
                    oldest = min(r.slot_start for r in slots if r.source == source)
                    floor = max(replace_from, oldest, int(now) - MAX_REPLACE_AGE)
                    self._conn.execute(
                        "DELETE FROM token_slots WHERE source = ? AND slot_start >= ?", (source, floor)
                    )
            if days:
                self._conn.executemany(
                    f"INSERT INTO token_usage (source, date, project, model, {self._COUNTS}, updated) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                    f"ON CONFLICT(source, date, project, model) DO UPDATE SET {self._MAX}",
                    [(r.source, r.date, r.project, r.model, *counts(r), now) for r in days],
                )
            if slots:
                self._conn.executemany(
                    f"INSERT INTO token_slots (source, slot_start, date, project, model, {self._COUNTS}, updated) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                    f"ON CONFLICT(source, slot_start, project, model) DO UPDATE SET {self._MAX}",
                    [(r.source, r.slot_start, r.date, r.project, r.model, *counts(r), now) for r in slots],
                )
                for source in {r.source for r in slots}:
                    first = min(r.date for r in slots if r.source == source)
                    self._conn.execute(
                        "DELETE FROM token_usage WHERE source = ? AND date >= ?", (source, first)
                    )

    def token_rows(self, since_slot: int = 0, since_date: str | None = "") -> list[TokenRow]:
        """Slot rows from since_slot on, plus old per-day rows from since_date on
        (skip those with since_date=None, e.g. for periods measured in hours)."""
        with self._lock:
            return self._token_rows(since_slot, since_date)

    def _token_rows(self, since_slot: int, since_date: str | None) -> list[TokenRow]:
        out = [
            TokenRow(source, date, project, model, *c, slot_start=slot)
            for source, slot, date, project, model, *c in self._conn.execute(
                f"SELECT source, slot_start, date, project, model, {self._COUNTS} FROM token_slots "
                "WHERE slot_start >= ? ORDER BY slot_start",
                (since_slot,),
            )
        ]
        if since_date is not None:
            out += [
                TokenRow(*row)
                for row in self._conn.execute(
                    f"SELECT source, date, project, model, {self._COUNTS} FROM token_usage "
                    "WHERE date >= ? ORDER BY date",
                    (since_date,),
                )
            ]
        return out

    def resolve_source(self, name: str) -> str:
        with self._lock:
            row = self._conn.execute("SELECT source FROM token_source_alias WHERE alias = ?", (name,)).fetchone()
        return row[0] if row else name

    def merge_token_sources(self, aliases: list[str], target: str) -> None:
        """Fold sources that are really one machine (macOS host names change with the
        network) into target. They report the same logs, so counts are merged with MAX,
        and later reports under an alias are stored under target too."""
        names = [a for a in aliases if a != target]
        if not names:
            return
        marks = ",".join("?" * len(names))
        with self._lock, self._conn:
            for table, key in (("token_slots", "slot_start, date"), ("token_usage", "date")):
                cols = f"source, {key}, project, model, {self._COUNTS}, updated"
                self._conn.execute(
                    f"INSERT INTO {table} ({cols}) "
                    f"SELECT ?, {key}, project, model, MAX(input), MAX(output), MAX(cache_write), "
                    f"MAX(cache_write_1h), MAX(cache_read), MAX(messages), MAX(updated) "
                    f"FROM {table} WHERE source IN ({marks}) GROUP BY {key}, project, model "
                    f"ON CONFLICT DO UPDATE SET {self._MAX}",
                    (target, *names),
                )
                self._conn.execute(f"DELETE FROM {table} WHERE source IN ({marks})", names)
            self._conn.executemany(
                "INSERT INTO token_source_alias (alias, source) VALUES (?, ?) "
                "ON CONFLICT(alias) DO UPDATE SET source = excluded.source",
                [(a, target) for a in names],
            )
            self._conn.execute("DELETE FROM token_source_alias WHERE alias = ?", (target,))

    def token_sources(self) -> list[dict]:
        with self._lock:
            return self._token_sources()

    def _token_sources(self) -> list[dict]:
        cur = self._conn.execute(
            "SELECT source, MAX(updated) FROM (SELECT source, updated FROM token_usage "
            "UNION ALL SELECT source, updated FROM token_slots) GROUP BY source ORDER BY source"
        )
        return [{"source": s, "updated_at": u} for s, u in cur]
