"""Token usage per project, model and day, reported by the Mac agent (mac/claude_tokens.py).

claude.ai's limit endpoint only knows percentages. Real token counts exist only in
Claude Code's local session logs, so a small script on the Mac sums them up and posts
the totals here. Costs are an estimate at public API list prices, not what a
subscription actually costs.
"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass

FIELDS = ("input", "output", "cache_write", "cache_write_1h", "cache_read", "messages")

# USD per million tokens: input, output, cache read. Cache writes are priced from
# input: 1.25x for the 5-minute cache, 2x for the 1-hour cache.
# Longest prefix wins, so specific versions go before the family fallback.
PRICES: list[tuple[str, float, float, float]] = [
    ("claude-fable-5-1", 10.0, 50.0, 0.25),
    ("claude-mythos-5-1", 10.0, 50.0, 0.25),
    ("claude-fable-5", 10.0, 50.0, 1.0),
    ("claude-mythos-5", 10.0, 50.0, 1.0),
    ("claude-opus-5-5", 4.0, 20.0, 0.20),
    ("claude-opus-5", 5.0, 25.0, 0.50),
    ("claude-opus-4-8", 5.0, 25.0, 0.50),
    ("claude-opus-4-7", 5.0, 25.0, 0.50),
    ("claude-opus-4-6", 5.0, 25.0, 0.50),
    ("claude-opus-4-5", 5.0, 25.0, 0.50),
    ("claude-opus-4", 15.0, 75.0, 1.50),
    ("claude-3-opus", 15.0, 75.0, 1.50),
    ("claude-sonnet-5-5", 2.0, 10.0, 0.20),
    ("claude-sonnet-5", 2.0, 10.0, 0.20),
    ("claude-sonnet-4", 3.0, 15.0, 0.30),
    ("claude-3-7-sonnet", 3.0, 15.0, 0.30),
    ("claude-3-5-sonnet", 3.0, 15.0, 0.30),
    ("claude-haiku-5-5", 0.10, 0.50, 0.01),
    ("claude-haiku-4-5", 1.0, 5.0, 0.10),
    ("claude-3-5-haiku", 0.80, 4.0, 0.08),
    ("claude-3-haiku", 0.25, 1.25, 0.03),
]


def price_for(model: str) -> tuple[float, float, float] | None:
    m = model.lower()
    best = None
    for prefix, inp, out, read in PRICES:
        if m.startswith(prefix) and (best is None or len(prefix) > len(best[0])):
            best = (prefix, inp, out, read)
    return best[1:] if best else None


def cost_parts(model: str, t: dict) -> dict[str, float] | None:
    """Estimated cost per token type: input, output, cache_write (5 min and 1 h), cache_read."""
    p = price_for(model)
    if p is None:
        return None
    inp, out, read = p
    cw1h = t.get("cache_write_1h", 0)
    cw5 = max(0, t.get("cache_write", 0) - cw1h)
    return {
        "input": t.get("input", 0) * inp / 1_000_000,
        "output": t.get("output", 0) * out / 1_000_000,
        "cache_write": (cw5 * inp * 1.25 + cw1h * inp * 2.0) / 1_000_000,
        "cache_read": t.get("cache_read", 0) * read / 1_000_000,
    }


def cost(model: str, t: dict) -> float | None:
    parts = cost_parts(model, t)
    return None if parts is None else sum(parts.values())


_VERSIONED = re.compile(r"^claude-(opus|sonnet|haiku|fable|mythos)-(\d+)(?:-(\d{1,2}))?(?:-\d{8})?$")
_OLD = re.compile(r"^claude-(\d)(?:-(\d))?-(opus|sonnet|haiku)(?:-\d{8})?$")


def pretty_model(model: str) -> str:
    """claude-opus-4-8 -> Opus 4.8, claude-3-5-haiku-20241022 -> Haiku 3.5."""
    m = _VERSIONED.match(model)
    if m:
        family, major, minor = m.groups()
        return f"{family.capitalize()} {major}" + (f".{minor}" if minor else "")
    m = _OLD.match(model)
    if m:
        major, minor, family = m.groups()
        return f"{family.capitalize()} {major}" + (f".{minor}" if minor else "")
    return model


@dataclass(frozen=True)
class TokenRow:
    source: str
    date: str      # YYYY-MM-DD in the Mac's local time
    project: str
    model: str
    input: int
    output: int
    cache_write: int
    cache_write_1h: int
    cache_read: int
    messages: int
    slot_start: int | None = None  # unix seconds of the 15-minute slot; None for old per-day reports


SLOT = 15 * 60


def _empty() -> dict:
    return {f: 0 for f in FIELDS}


def _add(acc: dict, r: TokenRow) -> None:
    for f in FIELDS:
        acc[f] += getattr(r, f)


def _finish(t: dict, model_costs: list[float | None]) -> dict:
    out = dict(t)
    out["total"] = t["input"] + t["output"] + t["cache_write"] + t["cache_read"]
    known = [c for c in model_costs if c is not None]
    out["cost"] = round(sum(known), 4) if known else None
    return out


def report(rows: list[TokenRow], bucket: Callable[[TokenRow], int], **meta) -> dict:
    """Group rows (already filtered to the period) by project, model and day.

    bucket maps a row to the unix start of its chart bar (an hour or a day); the result's
    "series" lists those bars, overall and per project. "daily" stays for older app builds.
    """
    # (project, model) -> totals; costs are per model, so everything is summed from this.
    pm: dict[tuple[str, str], dict] = defaultdict(_empty)
    pdm: dict[tuple[str, str, str], dict] = defaultdict(_empty)
    pbm: dict[tuple[str, int, str], dict] = defaultdict(_empty)
    for r in rows:
        _add(pm[(r.project, r.model)], r)
        _add(pdm[(r.project, r.date, r.model)], r)
        _add(pbm[(r.project, bucket(r), r.model)], r)

    def rollup(groups: dict[str, list[tuple[str, dict]]]) -> dict[str, dict]:
        out = {}
        for name, items in groups.items():
            acc = _empty()
            for _, t in items:
                for f in FIELDS:
                    acc[f] += t[f]
            out[name] = _finish(acc, [cost(model, t) for model, t in items])
        return out

    by_project: dict[str, list] = defaultdict(list)
    by_model: dict[str, list] = defaultdict(list)
    for (project, model), t in pm.items():
        by_project[project].append((model, t))
        by_model[model].append((model, t))

    proj_daily: dict[str, dict[str, list]] = defaultdict(lambda: defaultdict(list))
    all_daily: dict[str, list] = defaultdict(list)
    for (project, date, model), t in pdm.items():
        proj_daily[project][date].append((model, t))
        all_daily[date].append((model, t))

    proj_series: dict[str, dict[int, list]] = defaultdict(lambda: defaultdict(list))
    all_series: dict[int, list] = defaultdict(list)
    for (project, start, model), t in pbm.items():
        proj_series[project][start].append((model, t))
        all_series[start].append((model, t))

    def model_entry(model: str, t: dict) -> dict:
        return {"model": model, "name": pretty_model(model), **_finish(t, [cost(model, t)])}

    def sort_key(e: dict):
        return (-(e["cost"] or 0), -e["total"])

    projects = []
    for name, total in rollup(by_project).items():
        models = sorted((model_entry(m, t) for m, t in by_project[name]), key=sort_key)
        daily = [{"date": d, **v} for d, v in sorted(rollup(proj_daily[name]).items())]
        series = [{"start": b, **v} for b, v in sorted(rollup(proj_series[name]).items())]
        projects.append({"project": name, **total, "models": models, "daily": daily, "series": series})
    projects.sort(key=sort_key)

    models = []
    for model, items in by_model.items():
        acc = _empty()
        for _, t in items:
            for f in FIELDS:
                acc[f] += t[f]
        models.append(model_entry(model, acc))
    models.sort(key=sort_key)

    # Detail per model: its projects, its bars and the cost per token type.
    model_series: dict[str, dict[int, dict]] = defaultdict(lambda: defaultdict(_empty))
    for (project, start, model), t in pbm.items():
        acc = model_series[model][start]
        for f in FIELDS:
            acc[f] += t[f]

    by_model_detail = []
    for entry in models:
        model = entry["model"]
        parts = cost_parts(model, entry)
        projs = sorted(
            ({"project": p, **_finish(t, [cost(model, t)])} for (p, m), t in pm.items() if m == model),
            key=sort_key,
        )
        series = [
            {"start": b, **_finish(t, [cost(model, t)])} for b, t in sorted(model_series[model].items())
        ]
        by_model_detail.append({
            **entry,
            "costs": {k: round(v, 4) for k, v in parts.items()} if parts else None,
            "projects": projs,
            "series": series,
        })

    grand = _empty()
    for t in pm.values():
        for f in FIELDS:
            grand[f] += t[f]
    total = _finish(grand, [cost(m, t) for (_, m), t in pm.items()])

    return {
        **meta,
        "total": total,
        "projects": projects,
        "models": models,
        "by_model": by_model_detail,
        "daily": [{"date": d, **v} for d, v in sorted(rollup(all_daily).items())],
        "series": [{"start": b, **v} for b, v in sorted(rollup(all_series).items())],
    }
