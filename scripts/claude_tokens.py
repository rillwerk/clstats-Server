#!/usr/bin/env python3
"""Sum up Claude Code token usage per project, model and day and send it to the usage server.

Reads the session logs Claude Code writes on this Mac (~/.claude/projects/**/*.jsonl),
only the token counters, never message contents. Only the totals leave the Mac.

Config: ~/.config/claude-usage/config with SERVER_URL=... and API_TOKEN=...
(install.sh writes it), or the same names as environment variables. Behind Cloudflare
Access also CF_ACCESS_CLIENT_ID=... and CF_ACCESS_CLIENT_SECRET=... (service token).

  python3 claude_tokens.py            send to the server
  python3 claude_tokens.py --dry-run  print the totals instead
"""

from __future__ import annotations

import argparse
import json
import os
import re
import socket
import ssl
import sys
import urllib.error
import urllib.request
from collections import defaultdict
from datetime import datetime
from pathlib import Path

CONFIG = Path.home() / ".config" / "claude-usage" / "config"
CACHE = (
    Path.home() / "Library" / "Caches" if sys.platform == "darwin"
    else Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")
) / "claude-usage" / "tokens-cache.json"
CACHE_VERSION = 5
SLOT = 15 * 60  # seconds; the server shows 1 h / 6 h / 24 h from these slots


def log_roots() -> list[Path]:
    roots = []
    env = os.environ.get("CLAUDE_CONFIG_DIR")
    for base in ([Path(p) for p in env.split(",")] if env else []) + [
        Path.home() / ".claude",
        Path.home() / ".config" / "claude",
    ]:
        p = base.expanduser() / "projects"
        if p.is_dir() and p not in roots:
            roots.append(p)
    return roots


_project_names: dict[str, str | None] = {}
NO_PROJECT = "ohne Projekt"


def project_roots() -> list[Path]:
    """Folders that hold one project per subfolder. PROJECT_ROOTS (comma-separated) overrides."""
    env = os.environ.get("PROJECT_ROOTS")
    if env:
        return [Path(p).expanduser() for p in env.split(",") if p.strip()]
    home = Path.home()
    return [home / "Projects", home / "Projekte", home / "Developer", Path("/workspace")]


def repo_root(path: Path) -> Path | None:
    """Top folder of the git repository that contains path; worktrees map to their main repo."""
    home = Path.home()
    for p in [path, *path.parents]:
        if p == home or p == p.parent:
            break
        git = p / ".git"
        if git.is_dir():
            return p
        if git.is_file():
            # Worktree: ".git" holds "gitdir: /path/repo/.git/worktrees/<name>".
            try:
                target = git.read_text().split("gitdir:", 1)[1].strip()
                main = Path(target).parents[2]
                if main.name:
                    return main
            except (OSError, IndexError):
                pass
            return p
    return None


def folder_project(path: Path) -> str | None:
    """Project folder below one of the project roots (~/Projects/x/ios -> x). None for the
    root itself and for hidden folders such as .secrets: those are not projects."""
    for root in project_roots():
        try:
            rel = path.relative_to(root)
        except ValueError:
            continue
        if not rel.parts or rel.parts[0].startswith("."):
            return None
        top = root / rel.parts[0]
        # ~/Projects/claudeusageapp holding the repo ClaudeUsageApp: use the repo's spelling.
        try:
            for child in top.iterdir():
                if child.name.lower() == top.name.lower() and (child / ".git").exists():
                    return child.name
        except OSError:
            pass
        return top.name
    return None


def project_name(cwd: str | None) -> str | None:
    """Name of the repository or project folder Claude Code worked in, not the subfolder
    (src, api, ios ...). None when cwd says nothing about the project (a project root,
    a hidden folder, no cwd): the caller then keeps the session's project."""
    if not cwd:
        return None
    if cwd in _project_names:
        return _project_names[cwd]
    path = Path(cwd)
    if "/.worktrees/" in cwd:
        path = Path(cwd.split("/.worktrees/", 1)[0])
    name: str | None
    root = repo_root(path)
    if root is not None:
        name = folder_project(root) if root.name.startswith(".") else root.name
    else:
        under_root = any(path == r or r in path.parents for r in project_roots())
        if under_root:
            name = folder_project(path)
        elif path == Path.home() or any(part.startswith(".") for part in path.parts):
            name = None
        else:
            name = path.name or None
    _project_names[cwd] = name
    return name


def session_project(folder: Path) -> str:
    """Fallback from the log folder name (-Users-me-Projects-x encodes the start folder)."""
    for root in project_roots():
        enc = str(root).replace("/", "-").replace(".", "-")
        if folder.name == enc:
            return NO_PROJECT
        if folder.name.startswith(enc + "-"):
            return folder.name[len(enc) + 1:] or NO_PROJECT
    return folder.name.rsplit("-", 1)[-1] or NO_PROJECT


_PATH_KEYS = ("file_path", "path", "notebook_path")
# Absolute paths (also ~/... and $HOME/...) in a shell command, and the target of a cd.
_ABS_PATH = re.compile(r"""(?:^|[\s'"=:(])((?:~|\$HOME|\$\{HOME\})?/[^\s'"`;|&<>()]+)""")
_CD = re.compile(r"""(?:^|[;&|(]|\s)cd\s+['"]?([^\s'"`;|&<>()]+)""")


def _expand(p: str, cwd: str | None) -> Path | None:
    home = str(Path.home())
    for prefix in ("~", "$HOME", "${HOME}"):
        if p == prefix or p.startswith(prefix + "/"):
            p = home + p[len(prefix):]
            break
    if not p.startswith("/"):
        if not cwd or p.startswith("-") or "$" in p:
            return None
        p = os.path.join(cwd, p)
    return Path(os.path.normpath(p))


def tool_paths(msg: dict, cwd: str | None) -> list[Path]:
    """Paths a message's tool calls work on: file_path/path/notebook_path, and in shell
    commands the absolute paths and cd targets (relative ones against cwd)."""
    content = msg.get("content")
    if not isinstance(content, list):
        return []
    found: list[Path] = []
    for block in content:
        if not isinstance(block, dict) or block.get("type") != "tool_use":
            continue
        args = block.get("input")
        if not isinstance(args, dict):
            continue
        for k in _PATH_KEYS:
            v = args.get(k)
            if isinstance(v, str) and v:
                p = _expand(v, cwd)
                if p:
                    found.append(p)
        cmd = args.get("command")
        if isinstance(cmd, str):
            for m in _CD.finditer(cmd):
                p = _expand(m.group(1), cwd)
                if p:
                    found.append(p)
            for m in _ABS_PATH.finditer(cmd):
                p = _expand(m.group(1), cwd)
                if p:
                    found.append(p)
    return found


def path_project(path: Path) -> str | None:
    """Project of a path a tool touched, only below a project root (the first folder
    there, then the repository logic of project_name)."""
    for root in project_roots():
        try:
            rel = path.relative_to(root)
        except ValueError:
            continue
        if not rel.parts or rel.parts[0].startswith("."):
            return None
        # Only existing project folders: a relative path joined with a stale cwd, a glob
        # or a file in the root itself (~/Projects/ios, ~/Projects/x*, ~/Projects/a.md) is none.
        if not (root / rel.parts[0]).is_dir():
            return None
        # Up to three levels are enough to find the repository; keeps the name cache small.
        return project_name(str(root.joinpath(*rel.parts[:3])))
    return None


def parse_file(path: Path, folder: Path) -> dict[str, list]:
    """message key -> [slot, date, project, model, input, output, cache_write, cache_write_1h, cache_read]."""
    out: dict[str, list] = {}
    # message key -> project named by the message itself (cwd, else the paths its tools use)
    named: dict[str, str] = {}
    cwd = None
    with path.open("r", encoding="utf-8", errors="replace") as f:
        for line in f:
            if '"usage"' not in line:
                if cwd is None and '"cwd"' in line:
                    try:
                        cwd = json.loads(line).get("cwd")
                    except ValueError:
                        pass
                continue
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            cwd = entry.get("cwd") or cwd
            msg = entry.get("message")
            if not isinstance(msg, dict):
                continue
            usage = msg.get("usage")
            model = msg.get("model")
            if not isinstance(usage, dict) or not model or model.startswith("<"):
                continue
            ts = entry.get("timestamp")
            try:
                when = datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone()
            except (AttributeError, ValueError):
                continue
            slot = int(when.timestamp()) // SLOT * SLOT
            day = datetime.fromtimestamp(slot).date().isoformat()
            # A streamed answer is logged once per content block with the same counters,
            # and resumed sessions copy earlier messages: count each message once.
            key = f"{msg.get('id') or entry.get('uuid')}:{entry.get('requestId') or ''}"
            if key not in named:
                name = project_name(cwd)
                if name is None:
                    # Sessions started in the project root (~/Projects) often work there with
                    # absolute paths: the folders the tools touch name the project.
                    for p in tool_paths(msg, cwd):
                        name = path_project(p)
                        if name:
                            break
                if name:
                    named[key] = name
            created = usage.get("cache_creation") or {}
            row = [
                slot,
                day,
                None,
                model,
                int(usage.get("input_tokens") or 0),
                int(usage.get("output_tokens") or 0),
                int(usage.get("cache_creation_input_tokens") or 0),
                int(created.get("ephemeral_1h_input_tokens") or 0) if isinstance(created, dict) else 0,
                int(usage.get("cache_read_input_tokens") or 0),
            ]
            prev = out.get(key)
            out[key] = row if prev is None else prev[:4] + [max(a, b) for a, b in zip(prev[4:], row[4:])]

    # The working directory moves around (cd .secrets, cd ios ...). A message only switches
    # the session's project when it names one; otherwise it stays with the last one. Messages
    # before the first named project belong to that first project; a session that never
    # names one falls back to its log folder.
    first = next((named[k] for k in out if k in named), None) or session_project(folder)
    project = first
    for key, row in out.items():
        project = named.get(key) or project
        row[2] = project
    return out


def collect() -> list[dict]:
    try:
        cache = json.loads(CACHE.read_text())
        if cache.get("version") != CACHE_VERSION:
            cache = {}
    except (OSError, ValueError):
        cache = {}
    files_cache: dict = cache.get("files", {})
    new_cache: dict = {}

    for root in log_roots():
        for path in root.rglob("*.jsonl"):
            try:
                st = path.stat()
            except OSError:
                continue
            folder = path.relative_to(root).parts[0]
            sig = [st.st_size, st.st_mtime]
            hit = files_cache.get(str(path))
            if hit and hit.get("sig") == sig:
                new_cache[str(path)] = hit
                continue
            try:
                entries = parse_file(path, root / folder)
            except OSError:
                continue
            new_cache[str(path)] = {"sig": sig, "entries": entries}

    seen: set[str] = set()
    totals: dict[tuple, list[int]] = defaultdict(lambda: [0] * 6)
    for item in new_cache.values():
        for key, (slot, day, project, model, inp, out, cw, cw1h, cr) in item["entries"].items():
            if key in seen:
                continue
            seen.add(key)
            t = totals[(slot, day, project, model)]
            t[0] += inp
            t[1] += out
            t[2] += cw
            t[3] += cw1h
            t[4] += cr
            t[5] += 1

    try:
        CACHE.parent.mkdir(parents=True, exist_ok=True)
        tmp = CACHE.with_suffix(".tmp")
        tmp.write_text(json.dumps({"version": CACHE_VERSION, "files": new_cache}))
        tmp.replace(CACHE)
    except OSError as e:
        print(f"Cache nicht geschrieben: {e}", file=sys.stderr)

    return [
        {
            "slot_start": slot, "date": day, "project": project, "model": model,
            "input": t[0], "output": t[1], "cache_write": t[2], "cache_write_1h": t[3],
            "cache_read": t[4], "messages": t[5],
        }
        for (slot, day, project, model), t in sorted(totals.items())
    ]


def load_config() -> dict[str, str]:
    cfg = {}
    try:
        for line in CONFIG.read_text().splitlines():
            k, sep, v = line.strip().partition("=")
            if sep and not k.startswith("#"):
                cfg[k.strip()] = v.strip().strip('"').strip("'")
    except OSError:
        pass
    for k in ("SERVER_URL", "API_TOKEN", "SOURCE_NAME", "CF_ACCESS_CLIENT_ID", "CF_ACCESS_CLIENT_SECRET"):
        if os.environ.get(k):
            cfg[k] = os.environ[k]
    return cfg


def machine_name() -> str:
    """Stable name of this machine. On macOS the host name follows the network
    (WML-..., iot-..., a hotspot), which made one Mac show up as several sources."""
    if sys.platform == "darwin":
        import subprocess

        for key in ("LocalHostName", "ComputerName"):
            try:
                name = subprocess.run(
                    ["scutil", "--get", key], capture_output=True, text=True, timeout=5
                ).stdout.strip()
            except (OSError, subprocess.SubprocessError):
                name = ""
            if name:
                return name
    return socket.gethostname().split(".")[0]


def send(rows: list[dict], cfg: dict[str, str]) -> None:
    url = cfg["SERVER_URL"].rstrip("/") + "/api/tokens"
    source = cfg.get("SOURCE_NAME") or machine_name()
    payload: dict = {"source": source, "rows": rows}
    if rows:
        # Everything from the oldest log still on disk on is in this report: the server
        # replaces its rows there, so renamed projects are not counted twice. Older rows,
        # whose logs Claude Code already deleted, stay on the server.
        payload["replace_from"] = min(r["slot_start"] for r in rows)
    body = json.dumps(payload).encode()
    headers = {"Authorization": f"Bearer {cfg['API_TOKEN']}", "Content-Type": "application/json"}
    # Optional Cloudflare Access service token, for servers behind Cloudflare Access.
    if cfg.get("CF_ACCESS_CLIENT_ID") and cfg.get("CF_ACCESS_CLIENT_SECRET"):
        headers["CF-Access-Client-Id"] = cfg["CF_ACCESS_CLIENT_ID"]
        headers["CF-Access-Client-Secret"] = cfg["CF_ACCESS_CLIENT_SECRET"]
    req = urllib.request.Request(url, data=body, method="POST", headers=headers)
    # The python.org build on macOS ships without root certificates; use certifi when present.
    ctx = None
    try:
        import certifi  # type: ignore

        ctx = ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        pass
    with urllib.request.urlopen(req, timeout=60, context=ctx) as resp:
        print(f"{datetime.now():%Y-%m-%d %H:%M} gesendet: {len(rows)} Zeilen, Server: {resp.read().decode()}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true", help="nur ausgeben, nichts senden")
    args = ap.parse_args()

    if not log_roots():
        print("Keine Claude-Code-Logs gefunden (~/.claude/projects).", file=sys.stderr)
        return 1
    rows = collect()

    if args.dry_run:
        per_project: dict[str, int] = defaultdict(int)
        for r in rows:
            per_project[r["project"]] += r["input"] + r["output"] + r["cache_write"] + r["cache_read"]
        for name, n in sorted(per_project.items(), key=lambda x: -x[1]):
            print(f"{n:>15,}  {name}")
        print(f"{len(rows)} Zeilen (Viertelstunde x Projekt x Modell)")
        return 0

    cfg = load_config()
    if not cfg.get("SERVER_URL") or not cfg.get("API_TOKEN"):
        print(f"SERVER_URL und API_TOKEN fehlen, siehe {CONFIG}", file=sys.stderr)
        return 1
    try:
        send(rows, cfg)
    except urllib.error.HTTPError as e:
        print(f"Server antwortet {e.code}: {e.read().decode(errors='replace')[:300]}", file=sys.stderr)
        return 1
    except (urllib.error.URLError, OSError) as e:
        print(f"Server nicht erreichbar: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
