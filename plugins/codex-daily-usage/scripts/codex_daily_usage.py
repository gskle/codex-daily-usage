#!/usr/bin/env python3
"""Build a self-contained daily-by-model chart from local Codex session logs."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import html
import json
import math
import os
import re
import sqlite3
import subprocess
import sys
import csv
import base64
import urllib.request
import urllib.error
from collections import Counter
from pathlib import Path
from typing import Any

REPORT_TZ = dt.datetime.now().astimezone().tzinfo


def discover_ssh_hosts(codex_home: Path) -> tuple[list[str], str | None]:
    """Only discover hosts already registered with Codex, not arbitrary SSH hosts."""
    try:
        state = json.loads((codex_home / '.codex-global-state.json').read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return [], None
    hosts = {}
    for connection in state.get('codex-managed-remote-connections', []):
        if isinstance(connection, dict) and connection.get('alias'):
            hosts[connection.get('hostId')] = connection['alias']
    for project in state.get('remote-projects', []):
        host_id = project.get('hostId', '') if isinstance(project, dict) else ''
        if host_id.startswith('remote-ssh-discovered:'):
            hosts.setdefault(host_id, host_id.split(':', 1)[1])
    selected = hosts.get(state.get('selected-remote-host-id'))
    aliases = sorted(set(hosts.values()))
    return aliases, selected or (aliases[0] if len(aliases) == 1 else None)


PALETTE = [
    "#52a8ff",
    "#f08c58",
    "#44c3a1",
    "#c08af4",
    "#e3bd4f",
    "#ed7194",
    "#72c7da",
    "#a9c46c",
]


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _get_model(*objects: dict[str, Any]) -> str | None:
    for obj in objects:
        for key in ("model", "model_name", "model_id"):
            value = obj.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return None


def _first_string(objects: tuple[dict[str, Any], ...], keys: tuple[str, ...]) -> str | None:
    for obj in objects:
        for key in keys:
            value = obj.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return None


def _project_name(objects: tuple[dict[str, Any], ...], cwd: str | None) -> str | None:
    explicit = _first_string(objects, ("project_name", "workspace_name"))
    if explicit:
        normalized = explicit.replace("\\", "/").rstrip("/")
        return normalized.rsplit("/", 1)[-1]
    for obj in objects:
        for key in ("project", "workspace"):
            nested = _as_dict(obj.get(key))
            explicit = _first_string((nested,), ("name", "title"))
            if explicit:
                normalized = explicit.replace("\\", "/").rstrip("/")
                return normalized.rsplit("/", 1)[-1]
    return None


def _conversation_title(objects: tuple[dict[str, Any], ...]) -> str | None:
    title = _first_string(objects, ("conversation_title", "thread_title", "conversation_name", "thread_name", "title"))
    if title:
        return title
    for obj in objects:
        for key in ("thread", "conversation", "metadata"):
            nested = _as_dict(obj.get(key))
            title = _first_string((nested,), ("conversation_title", "thread_title", "name", "title"))
            if title:
                return title
    return None


def _first_user_text(payload: dict[str, Any]) -> str | None:
    content = payload.get("content")
    if isinstance(content, str):
        return content.strip() or None
    if not isinstance(content, list):
        return None
    for item in content:
        part = _as_dict(item)
        if part.get("type") in {"input_text", "text"}:
            text = part.get("text")
            if isinstance(text, str) and text.strip():
                return text.strip()
    return None


def _normalized_title(text: str | None) -> str:
    if not text:
        return ""
    return re.sub(r"\s+", " ", text).strip()


def _short_title(text: str | None) -> str:
    compact = _normalized_title(text)
    if not compact:
        return "未命名对话"
    if len(compact) > 80:
        compact = compact[:79].rstrip() + "…"
    return compact or "未命名对话"


def _normalize_path(value: str) -> str:
    return value.replace("\\", "/").rstrip("/").casefold()


def _thread_metadata(
    codex_home: Path,
    conversation_ids: set[str],
    conversation_cwds: dict[str, str],
) -> tuple[dict[str, str], dict[str, str]]:
    """Load only titles and project labels for known session IDs."""
    titles: dict[str, str] = {}
    projects: dict[str, str] = {}
    if not conversation_ids:
        return titles, projects

    index_path = codex_home / "session_index.jsonl"
    if index_path.is_file():
        try:
            with index_path.open("r", encoding="utf-8", errors="replace") as stream:
                for line in stream:
                    try:
                        entry = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(entry, dict):
                        continue
                    conversation_id = entry.get("id")
                    thread_name = entry.get("thread_name")
                    if conversation_id in conversation_ids and isinstance(thread_name, str) and thread_name.strip():
                        titles.setdefault(conversation_id, thread_name.strip())
        except OSError:
            pass

    for database_path in sorted(codex_home.glob("state_*.sqlite"), key=lambda item: item.stat().st_mtime, reverse=True):
        try:
            connection = sqlite3.connect(database_path.as_uri() + "?mode=ro", uri=True, timeout=2)
        except (OSError, sqlite3.Error):
            continue
        try:
            tables = {
                row[0]
                for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            project_names: dict[str, str] = {}
            project_roots: list[tuple[str, str]] = []
            if "projects" in tables:
                project_columns = {row[1] for row in connection.execute('PRAGMA table_info("projects")')}
                if {"id", "name"}.issubset(project_columns):
                    for project_id, project_name in connection.execute("SELECT id, name FROM projects"):
                        if isinstance(project_id, str) and isinstance(project_name, str) and project_name.strip():
                            project_names[project_id] = project_name.strip()
            if "project_roots" in tables:
                root_columns = {row[1] for row in connection.execute('PRAGMA table_info("project_roots")')}
                if {"project_id", "path"}.issubset(root_columns):
                    project_roots = [
                        (str(project_id), str(path))
                        for project_id, path in connection.execute("SELECT project_id, path FROM project_roots")
                        if project_id is not None and isinstance(path, str) and path.strip()
                    ]

            if "threads" not in tables:
                continue
            thread_columns = {row[1] for row in connection.execute('PRAGMA table_info("threads")')}
            if "id" not in thread_columns:
                continue
            optional_columns = [column for column in ("name", "title", "project_id", "cwd") if column in thread_columns]
            select_columns = ["id", *optional_columns]
            quoted_columns = ", ".join(f'"{column}"' for column in select_columns)
            id_list = sorted(conversation_ids)
            for offset in range(0, len(id_list), 400):
                batch = id_list[offset:offset + 400]
                placeholders = ",".join("?" for _ in batch)
                query = f"SELECT {quoted_columns} FROM threads WHERE id IN ({placeholders})"
                for row in connection.execute(query, batch):
                    values = dict(zip(select_columns, row))
                    conversation_id = values.get("id")
                    if not isinstance(conversation_id, str):
                        continue
                    thread_name = values.get("name") or titles.get(conversation_id) or _short_title(values.get("title"))
                    if isinstance(thread_name, str) and thread_name.strip():
                        titles[conversation_id] = thread_name.strip()

                    project_name = project_names.get(str(values.get("project_id")))
                    if not project_name:
                        cwd = values.get("cwd") or conversation_cwds.get(conversation_id)
                        if isinstance(cwd, str):
                            normalized_cwd = _normalize_path(cwd)
                            matching_roots = [
                                (project_id, path)
                                for project_id, path in project_roots
                                if normalized_cwd == _normalize_path(path)
                                or normalized_cwd.startswith(_normalize_path(path) + "/")
                            ]
                            if matching_roots:
                                root_project_id, _ = max(matching_roots, key=lambda item: len(item[1]))
                                project_name = project_names.get(root_project_id)
                    if project_name:
                        projects[conversation_id] = project_name
        except sqlite3.Error:
            continue
        finally:
            connection.close()

    # Desktop keeps remote task membership on the client, not in the server DB.
    try:
        state = json.loads((codex_home / '.codex-global-state.json').read_text(encoding='utf-8'))
        remote_projects = {
            item['id']: item.get('label') or item.get('name')
            for item in state.get('remote-projects', [])
            if isinstance(item, dict) and item.get('id')
        }
        assignments = state.get('thread-project-assignments', {})
        for cid in conversation_ids:
            assignment = assignments.get(cid, {}) if isinstance(assignments, dict) else {}
            if isinstance(assignment, dict):
                name = remote_projects.get(assignment.get('projectId'))
                if name:
                    projects[cid] = name
        projectless = state.get('projectless-thread-ids', [])
        if isinstance(projectless, list):
            for cid in conversation_ids.intersection(projectless):
                projects[cid] = ''
    except (OSError, ValueError, TypeError):
        pass
    return titles, projects


def _apply_thread_metadata(
    codex_home: Path,
    labels: dict[str, str],
    conversation_cwds: dict[str, str],
) -> dict[str, str]:
    titles, projects = _thread_metadata(codex_home, set(labels), conversation_cwds)
    resolved: dict[str, str] = {}
    for conversation_id, old_label in labels.items():
        old_project, separator, old_title = old_label.partition(" · ")
        if not separator:
            old_title = old_project
            old_project = ""
        metadata_title = titles.get(conversation_id)
        title = _normalized_title(metadata_title) if metadata_title else old_title
        project = projects.get(conversation_id, old_project)
        resolved[conversation_id] = f"{project} · {title}" if project else title
    return resolved


def _token_total(info: dict[str, Any]) -> int | None:
    usage = _as_dict(info.get("last_token_usage"))
    total = usage.get("total_tokens")
    if isinstance(total, (int, float)) and not isinstance(total, bool):
        return max(0, int(total))

    input_tokens = usage.get("input_tokens", usage.get("prompt_tokens"))
    output_tokens = usage.get("output_tokens", usage.get("completion_tokens"))
    if isinstance(input_tokens, (int, float)) and not isinstance(input_tokens, bool):
        if isinstance(output_tokens, (int, float)) and not isinstance(output_tokens, bool):
            return max(0, int(input_tokens) + int(output_tokens))
    return None


def _weekly_quota_snapshot(
    info: dict[str, Any],
    payload: dict[str, Any],
    record: dict[str, Any],
    stamp: dt.datetime,
) -> dict[str, Any] | None:
    limits = _as_dict(info.get("rate_limits") or payload.get("rate_limits") or record.get("rate_limits"))
    weekly = _as_dict(limits.get("secondary") or limits.get("weekly"))
    used = weekly.get("used_percent")
    window = weekly.get("window_minutes")
    if not isinstance(used, (int, float)) or isinstance(used, bool):
        return None
    if not isinstance(window, (int, float)) or window < 1440:
        return None
    return {
        "at": stamp.isoformat(),
        "used_percent": max(0.0, min(100.0, float(used))),
        "window_minutes": int(window),
        "resets_at": weekly.get("resets_at"),
    }


def _parse_timestamp(value: Any) -> dt.datetime | None:
    if not isinstance(value, (str, int, float)):
        return None
    try:
        if isinstance(value, (int, float)):
            stamp = dt.datetime.fromtimestamp(value, tz=dt.timezone.utc)
        else:
            text = value.strip()
            if not text:
                return None
            if text.endswith("Z"):
                text = text[:-1] + "+00:00"
            stamp = dt.datetime.fromisoformat(text)
            if stamp.tzinfo is None:
                stamp = stamp.astimezone()
        return stamp.astimezone(REPORT_TZ)
    except (OverflowError, OSError, ValueError):
        return None


def _local_codex_home() -> Path:
    configured = os.environ.get("CODEX_HOME")
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".codex"


def _timezone_label(prefix: str) -> str:
    offset = dt.datetime.now(REPORT_TZ).strftime("%z") or "+0000"
    if len(offset) == 5:
        offset = f"{offset[:3]}:{offset[3:]}"
    return f"{prefix}时区 UTC{offset}"


def _shell_quote(value: str) -> str:
    return "'" + value.replace("'", "'\"'\"'") + "'"


def _remote_usage_summary(args: argparse.Namespace) -> dict[str, Any]:
    if not args.ssh_host or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._@:-]*", args.ssh_host) is None:
        raise ValueError("请提供 SSH 配置别名或 user@host；别名仅支持字母、数字、点、短横线、下划线、@ 和冒号。")

    remote_args = ["-", "--json-summary", "--utc-offset=" + dt.datetime.now(REPORT_TZ).strftime("%z")]
    if args.account_scope:
        remote_args.append("--account-scope")
    if args.remote_codex_home:
        remote_args.extend(["--codex-home", args.remote_codex_home])
    if args.from_date:
        remote_args.extend(["--from-date", args.from_date])
    if args.to_date:
        remote_args.extend(["--to-date", args.to_date])
    if not args.from_date and not args.to_date:
        remote_args.extend(["--days", str(args.days)])

    arguments = " ".join(_shell_quote(value) for value in remote_args)
    if args.remote_python:
        remote_command = _shell_quote(args.remote_python) + " " + arguments
    else:
        candidates = 'python3.13 python3.12 python3.11 python3.10 python3.9 python3.8 python3 "$HOME"/.local/share/uv/python/*/bin/python3.* "$HOME"/miniconda3/bin/python "$HOME"/anaconda3/bin/python'
        version_check = _shell_quote('import sys; sys.exit(sys.version_info < (3, 8))')
        remote_command = ('for codex_usage_python in ' + candidates + '; do '
            'if "$codex_usage_python" -c ' + version_check + ' >/dev/null 2>&1; then '
            'exec "$codex_usage_python" ' + arguments + '; fi; done; '
            'echo "No Python >= 3.8 found; specify --remote-python." >&2; exit 69')
    try:
        completed = subprocess.run(
            ["ssh", "-T", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15", "-o", "StrictHostKeyChecking=yes", args.ssh_host, remote_command],
            input=Path(__file__).read_bytes(),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=180,
            check=False,
        )
    except FileNotFoundError as exc:
        raise RuntimeError("当前机器找不到 OpenSSH 的 ssh 命令。") from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("SSH 汇总超过 180 秒，已停止。") from exc

    if completed.returncode != 0:
        raise RuntimeError(
            f"远端汇总失败（退出码 {completed.returncode}）：" + completed.stderr.decode('utf-8', errors='replace')[-2000:]
        )
    try:
        summary = json.loads(completed.stdout.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("SSH 服务器没有返回有效的用量汇总。") from exc
    if (
        not isinstance(summary, dict)
        or not isinstance(summary.get("counts"), list)
        or not isinstance(summary.get("conversation_tokens"), list)
        or not isinstance(summary.get("conversation_labels"), list)
    ):
        raise RuntimeError("SSH 服务器返回的用量汇总格式不符合预期。")
    return summary


def _session_files(codex_home: Path) -> list[Path]:
    found: list[Path] = []
    for folder_name in ("sessions", "archived_sessions"):
        folder = codex_home / folder_name
        if folder.is_dir():
            found.extend(path for path in folder.rglob("*.jsonl") if path.is_file())
    return sorted(set(found))


def collect_usage(files: list[Path]) -> tuple[
    Counter[tuple[dt.date, str]],
    Counter[tuple[dt.date, str]],
    dict[str, str],
    dict[str, str],
    list[dict[str, Any]],
    int,
    int,
    int,
    list[dict[str, Any]],
    dict[str, str],
]:
    """Return call counts, date/session token counts, display labels, quota snapshots, and scan totals."""
    counts: Counter[tuple[dt.date, str]] = Counter()
    conversation_tokens: Counter[tuple[dt.date, str]] = Counter()
    conversation_labels: dict[str, str] = {}
    conversation_cwds: dict[str, str] = {}
    quota_snapshots: list[dict[str, Any]] = []
    counted = 0
    skipped = 0
    seen_event_lines: set[str] = set()
    usage_events: list[dict[str, Any]] = []
    conversation_kinds: dict[str, str] = {}

    for path in files:
        current_model: str | None = None
        cwd: str | None = None
        project: str | None = None
        explicit_title: str | None = None
        first_user_prompt: str | None = None
        conversation_id = path.stem
        previous_usage = None
        conversation_kind = "task"
        metadata_loaded = False
        created_at = None
        try:
            handle = path.open("r", encoding="utf-8", errors="replace")
        except OSError:
            skipped += 1
            continue

        with handle:
            for raw_line in handle:
                try:
                    record = json.loads(raw_line)
                except (json.JSONDecodeError, UnicodeError):
                    skipped += 1
                    continue
                if not isinstance(record, dict):
                    continue

                payload = _as_dict(record.get("payload"))
                info = _as_dict(payload.get("info"))
                event_type = payload.get("type") or record.get("type")
                model = _get_model(info, payload, record)
                metadata_objects = (info, payload, record)

                if event_type == "session_meta":
                    # id identifies this task; session_id can be shared by its
                    # parent and all guardian/subagent tasks.
                    # Later metadata may be embedded history from a fork.
                    if metadata_loaded:
                        continue
                    metadata_loaded = True
                    task_id = _first_string((payload, record), ("id", "session_id"))
                    if task_id:
                        conversation_id = task_id
                    created_at = _parse_timestamp(payload.get("timestamp") or record.get("timestamp"))
                    source_value = payload.get("source")
                    if isinstance(source_value, str):
                        try:
                            source_value = json.loads(source_value)
                        except ValueError:
                            source_value = {}
                    source = _as_dict(source_value)
                    conversation_kind = "guardian" if _as_dict(source.get("subagent")).get("other") == "guardian" else "task"

                current_cwd = _first_string(metadata_objects, ("cwd", "working_directory"))
                if current_cwd:
                    cwd = current_cwd
                candidate_project = _project_name(metadata_objects, cwd)
                if candidate_project:
                    project = candidate_project
                candidate_title = _conversation_title(metadata_objects)
                if candidate_title:
                    explicit_title = candidate_title
                if event_type in {"thread_name_updated", "conversation_title_updated"}:
                    renamed_title = _first_string(metadata_objects, ("name", "title"))
                    if renamed_title:
                        explicit_title = renamed_title
                if (
                    first_user_prompt is None
                    and payload.get("role") == "user"
                    and payload.get("type") == "message"
                ):
                    first_user_prompt = _first_user_text(payload)

                if event_type in {"turn_context", "turn_context_updated"}:
                    if model:
                        current_model = model
                    continue

                if event_type != "token_count":
                    if model and record.get("type") == "turn_context":
                        current_model = model
                    continue

                stamp = _parse_timestamp(record.get("timestamp") or payload.get("timestamp"))
                model = model or current_model or "(unknown model)"
                if stamp is None:
                    skipped += 1
                    continue

                snapshot = _weekly_quota_snapshot(info, payload, record, stamp)
                if snapshot is not None:
                    quota_snapshots.append(snapshot)
                cumulative = _as_dict(info.get("total_token_usage"))
                signature = json.dumps(cumulative, sort_keys=True) if cumulative else None
                # A quota-only refresh can repeat last_token_usage without a new call.
                if signature is not None and signature == previous_usage:
                    continue
                previous_usage = signature
                if created_at is not None and stamp < created_at:
                    continue
                token_total = _token_total(info)
                if token_total is None:
                    continue
                identity = {"at": stamp.isoformat(), "info": info}
                fingerprint = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
                if fingerprint in seen_event_lines:
                    continue
                seen_event_lines.add(fingerprint)
                counts[(stamp.date(), model)] += 1
                conversation_tokens[(stamp.date(), conversation_id)] += token_total
                last_usage = _as_dict(info.get("last_token_usage"))
                components = {
                    key: max(0, int(last_usage.get(key) or 0))
                    for key in ("input_tokens", "cached_input_tokens", "output_tokens", "reasoning_output_tokens")
                    if isinstance(last_usage.get(key), (int, float))
                }
                usage_events.append({"id": fingerprint, "date": stamp.date().isoformat(),
                    "model": model, "conversation_id": conversation_id, "tokens": token_total,
                    **components})
                counted += 1

        title = _normalized_title(explicit_title) if explicit_title else _short_title(first_user_prompt)
        conversation_labels[conversation_id] = f"{project} · {title}" if project else title
        conversation_kinds[conversation_id] = conversation_kind
        if cwd:
            conversation_cwds[conversation_id] = cwd

    return counts, conversation_tokens, conversation_labels, conversation_cwds, quota_snapshots, len(files), counted, skipped, usage_events, conversation_kinds


def _nice_step(maximum: int) -> int:
    if maximum <= 5:
        return 1
    raw = maximum / 5
    power = 10 ** math.floor(math.log10(raw))
    scaled = raw / power
    if scaled <= 1:
        factor = 1
    elif scaled <= 2:
        factor = 2
    elif scaled <= 5:
        factor = 5
    else:
        factor = 10
    return int(factor * power)


def _date_range(args: argparse.Namespace, dates: list[dt.date]) -> tuple[dt.date, dt.date]:
    today = dt.datetime.now(REPORT_TZ).date()
    if args.from_date or args.to_date:
        start = dt.date.fromisoformat(args.from_date) if args.from_date else (min(dates) if dates else today)
        end = dt.date.fromisoformat(args.to_date) if args.to_date else today
    elif args.days == 0:
        start = min(dates) if dates else today
        end = max(dates) if dates else today
    else:
        end = today
        start = end - dt.timedelta(days=args.days - 1)
    if start > end:
        raise ValueError("--from-date 必须早于或等于 --to-date")
    return start, end


def _chart_svg(
    dates: list[dt.date],
    models: list[str],
    values: dict[str, list[int]],
) -> str:
    width, height = 1040, 520
    left, right, top, bottom = 72, 28, 28, 72
    plot_w, plot_h = width - left - right, height - top - bottom
    max_value = max((value for series in values.values() for value in series), default=0)
    step = _nice_step(max_value or 1)
    y_max = max(step, math.ceil((max_value or 1) / step) * step)
    n_dates = max(1, len(dates))

    def x_at(index: int) -> float:
        return left + (plot_w / max(1, n_dates - 1)) * index

    def y_at(value: int) -> float:
        return top + plot_h - (value / y_max) * plot_h

    pieces = [
        f'<svg viewBox="0 0 {width} {height}" role="img" aria-label="每日模型调用数折线图" xmlns="http://www.w3.org/2000/svg">',
        '<rect x="0" y="0" width="100%" height="100%" fill="#10161f" rx="18"/>',
    ]

    for tick in range(0, y_max + 1, step):
        y = y_at(tick)
        pieces.append(f'<line x1="{left}" y1="{y:.1f}" x2="{width-right}" y2="{y:.1f}" stroke="#2a3543" stroke-width="1"/>')
        pieces.append(f'<text x="{left-14}" y="{y+4:.1f}" text-anchor="end" fill="#9aabba" font-size="12">{tick}</text>')

    label_every = max(1, math.ceil(n_dates / 10))
    for index, day in enumerate(dates):
        if index % label_every == 0 or index == n_dates - 1:
            x = x_at(index)
            pieces.append(f'<text x="{x:.1f}" y="{height-34}" text-anchor="middle" fill="#9aabba" font-size="11">{day.strftime("%m-%d")}</text>')

    for model_index, model in enumerate(models):
        series = values[model]
        color = PALETTE[model_index % len(PALETTE)]
        points = [(x_at(i), y_at(value), value) for i, value in enumerate(series)]
        if not points:
            continue
        path_data = " ".join(("M" if i == 0 else "L") + f" {x:.1f} {y:.1f}" for i, (x, y, _) in enumerate(points))
        pieces.append(f'<path d="{path_data}" fill="none" stroke="{color}" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"/>')
        for i, (x, y, value) in enumerate(points):
            if value == 0 and len(dates) > 45:
                continue
            day_text = dates[i].isoformat()
            title = html.escape(f"{day_text} · {model} · {value} 次")
            pieces.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="3.4" fill="{color}" stroke="#10161f" stroke-width="1.5"><title>{title}</title></circle>')
    pieces.append("</svg>")
    return "".join(pieces)


def build_report(
    counts: Counter[tuple[dt.date, str]],
    conversation_tokens: Counter[tuple[dt.date, str]],
    conversation_labels: dict[str, str],
    quota_snapshots: list[dict[str, Any]],
    start: dt.date,
    end: dt.date,
    files_scanned: int,
    counted_events: int,
    skipped_events: int,
    timezone_label: str,
    coverage_note: str = "",
    profile_reference: dict[str, Any] | None = None,
    background_tokens: Counter | None = None,
) -> str:
    dates: list[dt.date] = []
    day = start
    while day <= end:
        dates.append(day)
        day += dt.timedelta(days=1)

    model_totals: Counter[str] = Counter()
    for (day_key, model), count in counts.items():
        if start <= day_key <= end:
            model_totals[model] += count
    models = sorted(model_totals, key=lambda item: (-model_totals[item], item.casefold()))
    values = {model: [counts.get((date, model), 0) for date in dates] for model in models}
    total = sum(model_totals.values())
    active_days = sum(
        1 for date in dates if any(counts.get((date, model), 0) for model in models)
    )
    svg = _chart_svg(dates, models, values)

    summary_rows = "".join(
        f"<tr><td><span class='dot' style='background:{PALETTE[i % len(PALETTE)]}'></span>{html.escape(model)}</td><td>{model_totals[model]:,}</td></tr>"
        for i, model in enumerate(models)
    ) or "<tr><td colspan='2' class='muted'>所选日期范围内没有调用记录</td></tr>"

    detail_rows = []
    for date in reversed(dates):
        daily_models = [(model, counts.get((date, model), 0)) for model in models]
        daily_models = [(model, count) for model, count in daily_models if count]
        for model, count in daily_models:
            detail_rows.append(f"<tr><td>{date.isoformat()}</td><td>{html.escape(model)}</td><td>{count:,}</td></tr>")
    detail_html = "".join(detail_rows) or "<tr><td colspan='3' class='muted'>暂无数据</td></tr>"

    week_start = end - dt.timedelta(days=6)
    weekly_by_conversation: Counter[str] = Counter()
    relevant_conversations = {
        conversation_id
        for (date, conversation_id) in conversation_tokens
        if week_start <= date <= end or start <= date <= end
    }
    for (date, conversation_id), tokens in conversation_tokens.items():
        if week_start <= date <= end:
            weekly_by_conversation[conversation_id] += tokens
    display_labels = {
        conversation_id: conversation_labels.get(conversation_id, "未命名对话")
        for conversation_id in relevant_conversations
    }
    label_counts = Counter(display_labels.values())

    def conversation_label(conversation_id: str) -> str:
        label = display_labels.get(conversation_id, conversation_labels.get(conversation_id, "未命名对话"))
        if label_counts[label] > 1:
            label += f"（{conversation_id[-6:]}）"
        return html.escape(label)

    weekly_token_total = sum(weekly_by_conversation.values())
    reference_verified = profile_reference is not None and profile_reference.get('scope_verified')
    weekly_denominator = (profile_reference.get('week_total') if reference_verified else None) if profile_reference is not None else weekly_token_total
    share_header = '占账户近7天 tokens 比例' if profile_reference is not None else '占近7天用户任务日志比例'
    share_explanation = ('占比以兼容接口的账户近7天 token 总量为分母；后台审批和未归属差额单独列出。接口缺少日期或来源账户未核实时，占比显示为“—”。' if profile_reference is not None else '占比以已收集用户任务近7天 token 总量为分母。')
    daily_share_header = '占账户当日 tokens 比例' if profile_reference is not None else '占当日用户任务日志比例'
    background_tokens = background_tokens or Counter()
    daily_user_totals = Counter()
    for (day,cid),value in conversation_tokens.items():
        daily_user_totals[day] += value
    conversation_rows = []
    for conversation_id, tokens in sorted(weekly_by_conversation.items(), key=lambda item: (-item[1], item[0])):
        share = f'{100.0 * tokens / weekly_denominator:.3f}%' if weekly_denominator is not None and weekly_denominator > 0 else '—'
        today_tokens = conversation_tokens.get((end, conversation_id), 0)
        conversation_rows.append(
            f"<tr><td>{conversation_label(conversation_id)}</td><td>{today_tokens:,}</td><td>{tokens:,}</td><td>{share}</td></tr>"
        )
    if reference_verified and weekly_denominator is not None:
        bg_week = sum(v for (d,_),v in background_tokens.items() if week_start <= d <= end)
        bg_end = background_tokens.get((end, '__guardian__'), 0)
        ref_end = profile_reference['days'].get(end.isoformat())
        end_gap = '—' if ref_end is None else f'{ref_end-daily_user_totals[end]-bg_end:,}'
        for label, last_day, value in [('后台审批（汇总）', f'{bg_end:,}', bg_week), ('尚未归属的对账差额', end_gap, weekly_denominator-weekly_token_total-bg_week)]:
            share = f'{100.0*value/weekly_denominator:.3f}%' if weekly_denominator > 0 else '—'
            conversation_rows.append(f'<tr><td>{label}</td><td>{last_day}</td><td>{value:,}</td><td>{share}</td></tr>')
    conversation_table = "".join(conversation_rows) or "<tr><td colspan='4' class='muted'>所选近 7 天内没有可用的令牌记录</td></tr>"

    conversation_day_rows = []
    for (date, conversation_id), tokens in sorted(
        conversation_tokens.items(), key=lambda item: (item[0][0], item[0][1])
    ):
        if start <= date <= end and tokens:
            day_denominator = (profile_reference['days'].get(date.isoformat()) if reference_verified else None) if profile_reference is not None else daily_user_totals[date]
            share = f'{100.0*tokens/day_denominator:.3f}%' if day_denominator is not None and day_denominator > 0 else '—'
            conversation_day_rows.append(
                (tokens, date.isoformat(), conversation_id,
                 f"<tr><td>{date.isoformat()}</td><td>{conversation_label(conversation_id)}</td><td>{tokens:,}</td><td>{share}</td></tr>")
            )
    if reference_verified:
        for day in dates:
            ref_day = profile_reference['days'].get(day.isoformat())
            if ref_day is None:
                continue
            bg = background_tokens.get((day, '__guardian__'), 0)
            for label,value in [('后台审批（汇总）', bg), ('尚未归属的对账差额', ref_day-daily_user_totals[day]-bg)]:
                share = f'{100.0*value/ref_day:.3f}%' if ref_day > 0 else '—'
                conversation_day_rows.append((value, day.isoformat(), label, f'<tr><td>{day.isoformat()}</td><td>{label}</td><td>{value:,}</td><td>{share}</td></tr>'))
    conversation_day_table = "".join(row[3] for row in sorted(conversation_day_rows, key=lambda row: (-row[0], row[1], row[2]))) or "<tr><td colspan='3' class='muted'>所选日期范围内没有可用的令牌记录</td></tr>"

    latest_weekly_snapshot = None
    latest_weekly_snapshot_time = None
    for snapshot in quota_snapshots:
        stamp = _parse_timestamp(snapshot.get("at"))
        if stamp is not None and week_start <= stamp.date() <= end:
            if latest_weekly_snapshot_time is None or stamp > latest_weekly_snapshot_time:
                latest_weekly_snapshot = snapshot
                latest_weekly_snapshot_time = stamp
    quota_display = (
        f"{latest_weekly_snapshot['used_percent']:.1f}%"
        if latest_weekly_snapshot is not None
        else "无快照"
    )
    quota_note = (
        f"最近记录于 {html.escape(str(latest_weekly_snapshot['at']))}，这是账户级周额度快照。"
        if latest_weekly_snapshot is not None
        else "日志中没有近 7 天的周额度快照。"
    )

    return f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Codex 用量统计</title>
<style>
:root {{ color-scheme: dark; --bg:#0b1017; --card:#141c27; --line:#263241; --text:#edf3f8; --muted:#9aabba; --accent:#8ec5ff; }}
* {{ box-sizing:border-box; }}
body {{ margin:0; background:var(--bg); color:var(--text); font:15px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif; }}
main {{ max-width:1180px; margin:36px auto; padding:0 22px 50px; }}
h1 {{ margin:0 0 6px; font-size:28px; letter-spacing:-.02em; }}
.subtitle,.muted {{ color:var(--muted); }}
.cards {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(170px,1fr)); gap:12px; margin:24px 0; }}
.card,.panel {{ background:var(--card); border:1px solid var(--line); border-radius:16px; }}
.card {{ padding:16px 18px; }}
.card span {{ display:block; color:var(--muted); font-size:13px; }}
.card strong {{ display:block; margin-top:4px; font-size:25px; }}
.panel {{ padding:18px; margin-top:14px; }}
.chart {{ width:100%; overflow-x:auto; }}
.chart svg {{ display:block; width:100%; min-width:680px; height:auto; }}
.tables {{ display:grid; grid-template-columns:minmax(250px,.75fr) minmax(400px,1.5fr); gap:14px; }}
table {{ width:100%; border-collapse:collapse; font-size:14px; }}
th,td {{ padding:9px 10px; border-bottom:1px solid var(--line); text-align:left; }}
th {{ color:var(--muted); font-weight:600; }}
td:last-child,th:last-child {{ text-align:right; }}
.dot {{ display:inline-block; width:9px; height:9px; border-radius:50%; margin-right:9px; vertical-align:1px; }}
.note {{ margin-top:18px; padding:14px 16px; border-left:3px solid var(--accent); background:#101a25; color:var(--muted); border-radius:4px 12px 12px 4px; font-size:13px; }}
@media(max-width:760px) {{ .tables {{ grid-template-columns:1fr; }} main {{ margin-top:22px; }} }}
</style>
</head>
<body><main>
<h1>Codex 用量统计</h1>
<div class="subtitle">日期：{start.isoformat()} 至 {end.isoformat()}（{html.escape(timezone_label)}）</div>
<div class="note">{html.escape(coverage_note)}</div>
<section class="cards">
  <div class="card"><span>记录到的模型调用次数</span><strong>{total:,}</strong></div>
  <div class="card"><span>有记录的模型数</span><strong>{len(models):,}</strong></div>
  <div class="card"><span>有活动的日期</span><strong>{active_days:,}</strong></div>
  <div class="card"><span>扫描的会话文件数</span><strong>{files_scanned:,}</strong></div>
  <div class="card"><span>近 7 天用户任务令牌数</span><strong>{weekly_token_total:,}</strong></div>
  <div class="card"><span>最近记录的周额度使用率</span><strong>{quota_display}</strong></div>
</section>
<section class="panel"><h2>已收集日志的每日模型调用数</h2><div class="chart">{svg}</div></section>
<section class="panel"><h2>近 7 天各对话用量</h2><p class="muted">名称显示为“项目名 · 对话名”；没有项目名时只显示对话名。“区间结束日令牌数”使用报表区间的最后一天；{share_explanation}</p><table><thead><tr><th>项目与对话</th><th>区间结束日令牌数</th><th>近 7 天令牌数</th><th>{share_header}</th></tr></thead><tbody>{conversation_table}</tbody></table></section>
<section class="tables">
  <div class="panel"><h2>模型调用汇总</h2><table><thead><tr><th>模型</th><th>调用次数</th></tr></thead><tbody>{summary_rows}</tbody></table></div>
  <div class="panel"><h2>每日模型明细</h2><table><thead><tr><th>日期</th><th>模型</th><th>调用次数</th></tr></thead><tbody>{detail_html}</tbody></table></div>
</section>
<section class="panel"><h2>每日对话令牌明细</h2><table><thead><tr><th>日期</th><th>项目与对话</th><th>令牌数 ↓</th><th>{daily_share_header}</th></tr></thead><tbody>{conversation_day_table}</tbody></table></section>
<div class="note">模型调用次数按含用量的 token_count 事件计算，已排除累计用量未变化的重复快照和重复事件；令牌数读取事件里的 last_token_usage.total_tokens，缺少该字段时按输入与输出令牌数相加。项目名和对话名优先读取 Codex 索引/状态元数据；仅使用有记录的项目名；对话标题缺失时取首条用户请求的前 80 个字符。Codex 日期目录下的无项目会话只显示对话名。对话占比的分母见表头；账户token占比不能精确换算成订阅周额度消耗。{quota_note}这不是服务器账单；云端任务、其他设备以及未保留的日志可能不在其中。共扫描 {files_scanned} 个会话文件，解析到 {counted_events} 条调用事件；所选日期内有 {total} 次调用；另有 {skipped_events} 条无法解析或缺少时间的记录被跳过。</div>
</main></body></html>"""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="按日期汇总本机或 SSH 服务器上的 Codex 用量并生成中文报表。")
    parser.add_argument("--codex-home", type=Path, help="本机 Codex Home；默认读取 CODEX_HOME 或 ~/.codex")
    parser.add_argument("--days", type=int, default=30, help="统计天数；0 表示所有已记录日期")
    parser.add_argument("--from-date", help="起始日期（含），格式 YYYY-MM-DD")
    parser.add_argument("--to-date", help="结束日期（含），格式 YYYY-MM-DD")
    parser.add_argument("--ssh-host", help="SSH 配置别名或 user@host，用于读取远端 Codex 日志")
    parser.add_argument("--local", action="store_true", help="明确选择本机 Codex 日志作为数据源")
    parser.add_argument("--all-sources", action="store_true", help="合并本机和所有已在 Codex 注册的 SSH 主机")
    parser.add_argument("--remote-python", help="远端 Python 解释器路径；默认自动寻找 Python 3.8+（含 uv 安装）")
    parser.add_argument("--summary-output", type=Path, help="另存只含用量和名称的 JSON 汇总")
    parser.add_argument("--profile-reference", action="store_true", help="以本人 Profile 兼容接口的账户 tokens 为显示基准，按 UTC 统计")
    parser.add_argument("--account-scope", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--utc-offset", help="统计日界线，例如 +0800；默认本机时区")
    parser.add_argument("--remote-codex-home", help="远端 Codex Home；默认 ~/.codex")
    parser.add_argument("--output", type=Path, help="报表保存路径；默认保存到 outputs/codex-daily-usage.html")
    parser.add_argument("--json-summary", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.profile_reference:
        if args.utc_offset not in (None, '+0000', '-0000'):
            parser.error('--profile-reference 必须按 UTC 统计；本地自然日统计请省略此选项')
        args.utc_offset = '+0000'
        args.account_scope = True
        if not (args.local or args.ssh_host or args.all_sources):
            args.all_sources = True
    if args.days < 0:
        parser.error("--days 必须为 0 或正整数")
    if (args.from_date or args.to_date) and args.days != 30:
        parser.error("--days 与 --from-date/--to-date 不能同时使用")
    if args.ssh_host and args.codex_home:
        parser.error("使用 --ssh-host 时请指定 --remote-codex-home；--codex-home 仅用于本机日志")
    if args.ssh_host and args.local:
        parser.error("--ssh-host 和 --local 不能同时使用；请一次选择一个数据源")
    if args.ssh_host and args.json_summary:
        parser.error("--json-summary 为内部选项，不能与 --ssh-host 同时使用")
    if args.all_sources and (args.ssh_host or args.local or args.json_summary):
        parser.error("--all-sources 不能与 --ssh-host、--local 或内部汇总模式同时使用")
    if not args.ssh_host and not args.local and not args.json_summary and not args.all_sources:
        aliases, selected = discover_ssh_hosts(_local_codex_home())
        if not selected:
            parser.error("无法唯一确定已配置的 Codex SSH 主机。候选：" + ", ".join(aliases) + "；请指定 --ssh-host，或用 --all-sources 合并已配置来源。")
        args.ssh_host = selected
    if args.codex_home and not (args.local or args.json_summary):
        parser.error("--codex-home 仅用于显式本机模式（--local）或内部汇总模式")
    return args



def local_summary(args: argparse.Namespace) -> dict[str, Any]:
    codex_home = (args.codex_home or _local_codex_home()).expanduser().resolve()
    files = _session_files(codex_home)
    if not files:
        raise RuntimeError(f"没有找到 Codex 会话日志：{codex_home}")
    counts, tokens, labels, cwds, snapshots, scanned, counted, skipped, events, kinds = collect_usage(files)
    labels = _apply_thread_metadata(codex_home, labels, cwds)
    start, end = _date_range(args, [day for day, _ in counts])
    token_start = min(start, end - dt.timedelta(days=6))
    events = [e for e in events if token_start <= dt.date.fromisoformat(e['date']) <= end]
    relevant = {e['conversation_id'] for e in events}
    return {
        "start": start.isoformat(), "end": end.isoformat(), "timezone_label": _timezone_label("统计"),
        "account_scope": account_scope(codex_home) if getattr(args, 'account_scope', False) else None,
        "codex_home": str(codex_home), "files_scanned": scanned, "counted_events": counted, "skipped_events": skipped,
        "counts": [{"date": d.isoformat(), "model": m, "calls": n} for (d,m),n in counts.items() if start <= d <= end],
        "conversation_tokens": [{"date": d.isoformat(), "conversation_id": cid, "tokens": n} for (d,cid),n in tokens.items() if token_start <= d <= end],
        "conversation_labels": [{"conversation_id": cid, "label": labels[cid]} for cid in sorted(relevant)],
        "conversation_kinds": {cid:kinds.get(cid, 'task') for cid in relevant},
        "weekly_quota_snapshots": [x for x in snapshots if token_start <= _parse_timestamp(x['at']).date() <= end],
        "events": events,
    }


def merged_summary(sources: list[dict[str, Any]]) -> dict[str, Any]:
    result = dict(sources[0])
    unique, labels, kinds = {}, {}, {}
    for source in sources:
        for event in source['events']:
            if event['id'] not in unique:
                unique[event['id']] = dict(event, sources=[source.get('source_label', '未指定来源')])
            elif source.get('source_label', '未指定来源') not in unique[event['id']]['sources']:
                unique[event['id']]['sources'].append(source.get('source_label', '未指定来源'))
        labels.update({x['conversation_id']:x['label'] for x in source['conversation_labels']})
        kinds.update(source.get('conversation_kinds', {}))
    labels = _apply_thread_metadata(_local_codex_home(), labels, {})
    calls, tokens = Counter(), Counter()
    for event in unique.values():
        calls[(event['date'], event['model'])] += 1
        tokens[(event['date'], event['conversation_id'])] += event['tokens']
    result.update({
        'events': list(unique.values()),
        'counts': [{'date':d, 'model':m, 'calls':v} for (d,m),v in calls.items()],
        'conversation_tokens': [{'date':d, 'conversation_id':cid, 'tokens':v} for (d,cid),v in tokens.items()],
        'conversation_labels': [{'conversation_id':cid, 'label':label} for cid,label in labels.items()],
        'conversation_kinds': kinds,
        'weekly_quota_snapshots': [x for source in sources for x in source['weekly_quota_snapshots']],
        'files_scanned': sum(x['files_scanned'] for x in sources),
        'counted_events': len(unique), 'skipped_events': sum(x['skipped_events'] for x in sources),
        'cross_source_duplicates': sum(len(x['events']) for x in sources)-len(unique),
    })
    result.pop('account_scope', None)
    return result


def account_scope(codex_home: Path, auth: dict[str, Any] | None = None) -> dict[str, str] | None:
    """Return only identity hashes; never return authentication credentials."""
    try:
        if auth is None:
            auth = json.loads((codex_home / 'auth.json').read_text(encoding='utf-8'))
        tokens = _as_dict(auth.get('tokens'))
        account = tokens.get('account_id')
        claims = {}
        id_token = tokens.get('id_token')
        if isinstance(id_token, str):
            part = id_token.split('.')[1]
            claims = json.loads(base64.urlsafe_b64decode(part + '=' * (-len(part) % 4)))
        details = _as_dict(claims.get('https://api.openai.com/auth'))
        user = details.get('chatgpt_user_id') or claims.get('sub')
        account = account or details.get('chatgpt_account_id')
        if not isinstance(account, str) or not isinstance(user, str):
            return None
        return {'account': hashlib.sha256(account.encode()).hexdigest(),
                'user': hashlib.sha256(user.encode()).hexdigest()}
    except (OSError, ValueError, IndexError, TypeError, AttributeError):
        return None


class NoProfileRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Credentials are sent only to the fixed official destination below.
        return None


def fetch_profile_reference(codex_home: Path) -> dict[str, Any]:
    result = {'source': 'Profile 兼容统计接口', 'timezone': 'UTC',
              'fetched_at': dt.datetime.now(dt.timezone.utc).isoformat(), 'status': 'unavailable', 'days': {}}
    try:
        auth = json.loads((codex_home / 'auth.json').read_text(encoding='utf-8'))
        credentials = _as_dict(auth.get('tokens'))
        access = credentials.get('access_token')
        if not isinstance(access, str) or not access:
            result['error'] = '没有可用的现有 ChatGPT 登录'
            return result
        headers = {'Authorization': 'Bearer ' + access, 'Accept': 'application/json',
                   'User-Agent': 'codex-daily-usage/1.0'}
        if isinstance(credentials.get('account_id'), str):
            headers['ChatGPT-Account-Id'] = credentials['account_id']
        request = urllib.request.Request('https://chatgpt.com/backend-api/wham/profiles/me', headers=headers, method='GET')
        with urllib.request.build_opener(NoProfileRedirect).open(request, timeout=30) as response:
            data = json.load(response)
        if _as_dict(data.get('metadata')).get('stats_error'):
            result['error'] = '账户统计服务报告数据错误'
            return result
        buckets = _as_dict(data.get('stats')).get('daily_usage_buckets')
        if not isinstance(buckets, list):
            result['error'] = '账户统计接口未返回每日数据'
            return result
        days = {}
        for item in buckets:
            if not isinstance(item, dict):
                continue
            day, value = item.get('start_date'), item.get('tokens')
            if not isinstance(day, str) or not isinstance(value, int) or isinstance(value, bool) or value < 0:
                continue
            dt.date.fromisoformat(day)
            if day in days and days[day] != value:
                raise ValueError('conflicting daily buckets')
            days[day] = value
        result.update(status='ok', days=days, _scope=account_scope(codex_home, auth))
    except urllib.error.HTTPError as exc:
        result['error'] = f'账户统计请求失败：HTTP {exc.code}'
        if exc.fp is not None:
            exc.close()
    except (OSError, ValueError, TypeError, AttributeError):
        result['error'] = '无法读取账户统计；请检查现有登录和网络连接'
    return result


def profile_reference_for_period(reference, start, end, summaries):
    result = dict(reference)
    scope = result.pop('_scope', None)
    result['scope_verified'] = scope is not None and all(x.get('account_scope') == scope for x in summaries)
    week_start = end - dt.timedelta(days=6)
    all_dates = []
    day = min(start, week_start)
    while day <= end:
        all_dates.append(day.isoformat())
        day += dt.timedelta(days=1)
    result['days'] = {d: reference.get('days', {}).get(d) for d in all_dates}
    period_dates = [d for d in all_dates if start.isoformat() <= d <= end.isoformat()]
    week_dates = [d for d in all_dates if week_start.isoformat() <= d <= end.isoformat()]
    missing = [d for d in period_dates if result['days'][d] is None]
    result['missing_dates'] = missing
    result['period_reported_total'] = sum(result['days'][d] for d in period_dates if result['days'][d] is not None)
    result['period_total'] = None if missing or result['status'] != 'ok' else result['period_reported_total']
    result['week_total'] = None if any(result['days'][d] is None for d in week_dates) or result['status'] != 'ok' else sum(result['days'][d] for d in week_dates)
    return result


def profile_reference_html(reference, events, start, end):
    if reference['status'] != 'ok':
        return '<section class="panel"><h2>账户 token 用量（兼容接口）</h2><p>暂不可用：' + html.escape(reference.get('error', '接口未返回')) + '。以下日志数值仅代表已收集记录。</p></section>'
    daily_logs = Counter()
    for event in events:
        if start.isoformat() <= event['date'] <= end.isoformat():
            daily_logs[event['date']] += event['tokens']
    days = {d:v for d,v in reference['days'].items() if start.isoformat() <= d <= end.isoformat()}
    verified = reference['scope_verified']
    if not any(v is not None for v in days.values()):
        return '<section class="panel"><h2>账户 token 用量（兼容接口）</h2><p>所选日期的账户统计尚未返回，未按零计入。以下仅列已收集日志。</p></section>'
    rows = []
    for day, account_tokens in sorted(days.items(), reverse=True):
        logs = daily_logs[day]
        ref_text = '未返回' if account_tokens is None else f'{account_tokens:,}'
        gap = '—' if account_tokens is None or not verified else f'{account_tokens-logs:,}'
        rows.append(f'<tr><td>{day}</td><td>{ref_text}</td><td>{logs:,}</td><td>{gap}</td></tr>')
    complete = reference['period_total'] is not None
    label = '账户 token 总量（兼容接口）' if complete else '账户 tokens（仅接口已返回日期）'
    available_logs = sum(daily_logs[d] for d,v in days.items() if v is not None)
    gap_value = f"{reference['period_reported_total']-available_logs:,}" if verified else '未核实来源账户'
    note = '按 UTC 日期统计；账户显示值直接来自兼容接口，各任务数值保留日志原值，差额单列且不分摊。'
    if not complete:
        note += ' 尚未返回的日期：' + '、'.join(reference['missing_dates']) + '；未按零计入。'
    if not verified:
        note += ' 日志来源未全部核实为同一账户，暂不计算账户占比和差额。'
    note += ' 数据获取时间：' + reference['fetched_at']
    return (f'<section class="panel"><h2>{label}</h2><div class="cards">'
        f'<div class="card"><span>{label}</span><strong>{reference["period_reported_total"]:,}</strong></div>'
        f'<div class="card"><span>对应日期已收集日志 tokens</span><strong>{available_logs:,}</strong></div>'
        f'<div class="card"><span>尚未归属到任务的差额</span><strong>{gap_value}</strong></div></div>'
        f'<p class="muted">{html.escape(note)}</p><table><thead><tr><th>UTC 日期</th><th>账户 tokens</th><th>日志 tokens</th><th>对账差额</th></tr></thead><tbody>'
        + ''.join(rows) + '</tbody></table></section>')


def main() -> int:
    global REPORT_TZ
    args = parse_args()
    if args.utc_offset:
        if not re.fullmatch(r'[+-]\d{4}', args.utc_offset):
            print('--utc-offset 必须为 +0800 这样的格式', file=sys.stderr)
            return 2
        hours, minutes = int(args.utc_offset[1:3]), int(args.utc_offset[3:5])
        if hours > 23 or minutes > 59:
            return 2
        REPORT_TZ = dt.timezone(dt.timedelta(minutes=(hours*60+minutes)*(1 if args.utc_offset[0]=='+' else -1)))
    if args.json_summary:
        try:
            print(json.dumps(local_summary(args), ensure_ascii=False, separators=(',', ':')))
            return 0
        except (OSError, ValueError, RuntimeError) as exc:
            print(str(exc), file=sys.stderr)
            return 2

    source_specs = []
    if args.all_sources:
        source_specs = ['本机', *discover_ssh_hosts(_local_codex_home())[0]]
    else:
        source_specs = [args.ssh_host] if args.ssh_host else ['本机']
    summaries, coverage = [], []
    for source in source_specs:
        try:
            if source == '本机':
                summary = local_summary(args)
            else:
                remote_args = argparse.Namespace(**vars(args))
                remote_args.ssh_host = source
                summary = _remote_usage_summary(remote_args)
            summary['source_label'] = source
            summaries.append(summary)
            coverage.append({'source':source,'status':'ok','codex_home':summary['codex_home'],'files':summary['files_scanned']})
        except (OSError, ValueError, KeyError, RuntimeError) as exc:
            coverage.append({'source':source, 'status':'error', 'error':str(exc)})
    if not summaries:
        print(json.dumps(coverage, ensure_ascii=False), file=sys.stderr)
        return 2
    merged = merged_summary(summaries)
    merged['sources'] = coverage
    start, end = dt.date.fromisoformat(merged['start']), dt.date.fromisoformat(merged['end'])
    profile = None
    if args.profile_reference:
        profile = profile_reference_for_period(fetch_profile_reference(_local_codex_home()), start, end, summaries)
        merged['profile_reference'] = profile
    counts = Counter({(dt.date.fromisoformat(x['date']), x['model']):x['calls'] for x in merged['counts']})
    labels = {x['conversation_id']:x['label'] for x in merged['conversation_labels']}
    tokens, background_tokens = Counter(), Counter()
    guardians = set()
    for item in merged['conversation_tokens']:
        cid, day = item['conversation_id'], dt.date.fromisoformat(item['date'])
        if merged['conversation_kinds'].get(cid)=='guardian':
            guardians.add(cid)
            background_tokens[(day, '__guardian__')] += item['tokens']
        else:
            tokens[(day,cid)] += item['tokens']
    # Keep backend approval runs out of the list of user-visible tasks.
    user_calls = Counter()
    for event in merged['events']:
        if merged['conversation_kinds'].get(event['conversation_id']) != 'guardian':
            user_calls[(dt.date.fromisoformat(event['date']), event['model'])] += 1
    counts = user_calls
    week_start = end-dt.timedelta(days=6)
    weekly = Counter()
    for (day,cid),value in tokens.items():
        if week_start <= day <= end:
            weekly[cid] += value
    denominator = sum(weekly.values())
    coverage_note = '数据来源：' + '；'.join(f"{x['source']}（{x.get('files',0)} 个日志文件）" if x['status']=='ok' else f"{x['source']}：读取失败，未计入" for x in coverage)
    background_week = sum(v for (d,_),v in background_tokens.items() if week_start <= d <= end)
    coverage_note += f'。统一按 {_timezone_label("统计")} 划分日期。后台审批（guardian）共 {len(guardians)} 个内部任务，近 7 天记录 {background_week:,} tokens，已单独列出。此报表覆盖上述已配置来源，不能保证包括未接入设备或云端任务。'
    output = args.output or ((Path.cwd()/'outputs' if (Path.cwd()/'outputs').is_dir() else Path.cwd())/'codex-daily-usage.html')
    output = output.expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    document = build_report(counts, tokens, labels, merged['weekly_quota_snapshots'], start,end,merged['files_scanned'],sum(counts.values()),merged['skipped_events'],_timezone_label('统计'),coverage_note,profile_reference=profile,background_tokens=background_tokens)
    if guardians:
        background_rows = ''.join(f'<tr><td>{day}</td><td>{value:,}</td></tr>' for (day,_),value in sorted(background_tokens.items()) if start <= day <= end)
        background_section = '<section class="panel"><details><summary>系统后台审批用量（guardian，未计入上方任务表）</summary><table><thead><tr><th>日期</th><th>记录的 tokens</th></tr></thead><tbody>'+background_rows+'</tbody></table></details></section>'
        document = document.replace('</main>', background_section+'</main>')
    period_events = [e for e in merged['events'] if start.isoformat() <= e['date'] <= end.isoformat()]
    period_all = sum(e['tokens'] for e in period_events)
    period_background = sum(e['tokens'] for e in period_events if merged['conversation_kinds'].get(e['conversation_id']) == 'guardian')
    reconciliation = {
        'total_tokens': period_all,
        'user_tokens': period_all - period_background,
        'background_tokens': period_background,
        'input_tokens': sum(e.get('input_tokens', 0) for e in period_events),
        'cached_input_tokens': sum(e.get('cached_input_tokens', 0) for e in period_events),
        'output_tokens': sum(e.get('output_tokens', 0) for e in period_events),
        'calls': len(period_events),
    }
    summary_cards = '<section class="cards">' + ''.join(
        f'<div class="card"><span>{name}</span><strong>{value:,}</strong></div>'
        for name, value in [('所选日期全部记录 tokens', period_all), ('所选日期用户任务 tokens', period_all-period_background), ('所选日期后台审批 tokens', period_background), ('所选日期缓存输入 tokens（单列核对）', reconciliation['cached_input_tokens'])]
    ) + '</section>'
    document = document.replace('<section class="cards">', summary_cards + '<section class="cards">', 1)
    merged['period_totals'] = reconciliation
    merged['collector_version'] = 'task-id-v2'
    if profile is not None:
        account_section = profile_reference_html(profile, merged['events'], start, end)
        document = document.replace('<section class="cards">', account_section + '<section class="cards">', 1)
        merged['display_total_tokens'] = profile['period_total']
        merged['display_source'] = profile['source']
    else:
        merged['display_total_tokens'] = period_all
        merged['display_source'] = '已收集日志'
    output.write_text(document, encoding='utf-8')
    csv_path = output.with_suffix('.csv')
    with csv_path.open('w', encoding='utf-8-sig', newline='') as stream:
        writer = csv.writer(stream)
        share_name = '占账户近7天tokens比例' if profile is not None else '占近7天用户任务令牌比例'
        share_base = (profile.get('week_total') if profile.get('scope_verified') else None) if profile is not None else denominator
        writer.writerow(['项目与对话','区间结束日令牌数','近7天令牌数',share_name])
        for cid,value in weekly.most_common():
            label = labels.get(cid,'未命名对话')
            if label.startswith(('=', '+', '-', '@')):
                label = "'"+label
            writer.writerow([label,tokens.get((end,cid),0),value,f'{value/share_base:.6%}' if share_base is not None and share_base > 0 else ''])
        if profile is not None and profile.get('scope_verified') and share_base is not None:
            user_end = sum(v for (d,_),v in tokens.items() if d==end)
            bg_end = background_tokens.get((end,'__guardian__'),0)
            ref_end = profile['days'].get(end.isoformat())
            for label,day_value,week_value in [('后台审批（汇总）',bg_end,background_week),('尚未归属的对账差额',None if ref_end is None else ref_end-user_end-bg_end,share_base-denominator-background_week)]:
                writer.writerow([label,day_value,week_value,f'{week_value/share_base:.6%}' if share_base > 0 else ''])
    daily_csv_path = output.with_suffix('.daily.csv')
    with daily_csv_path.open('w', encoding='utf-8-sig', newline='') as stream:
        writer = csv.writer(stream)
        share_name = '占账户当日tokens比例' if profile is not None else '占当日用户任务日志比例'
        writer.writerow(['日期','类别','项目与任务','记录tokens或对账差额',share_name])
        csv_daily_rows = []
        days = sorted({d for d,_ in tokens if start <= d <= end} | {d for d,_ in background_tokens if start <= d <= end})
        if profile is not None:
            days = sorted(set(days) | {dt.date.fromisoformat(d) for d in profile['days'] if start.isoformat() <= d <= end.isoformat()})
        for day in days:
            day_users = [(cid,n) for (d,cid),n in tokens.items() if d==day]
            day_total = sum(n for _,n in day_users)
            base = (profile['days'].get(day.isoformat()) if profile.get('scope_verified') else None) if profile is not None else day_total
            day_rows = [('用户任务', labels.get(cid,'未命名任务'),n) for cid,n in sorted(day_users,key=lambda x:-x[1])]
            if profile is not None and profile.get('scope_verified'):
                bg = background_tokens.get((day,'__guardian__'),0)
                day_rows.append(('后台审批','后台审批（汇总）',bg))
                if base is not None:
                    day_rows.append(('对账差额','尚未归属的对账差额',base-day_total-bg))
            for kind,label,value in day_rows:
                if label.startswith(('=', '+', '-', '@')):
                    label = "'" + label
                csv_daily_rows.append([day.isoformat(),kind,label,value,f'{value/base:.6%}' if base is not None and base>0 else ''])
        writer.writerows(sorted(csv_daily_rows, key=lambda row: (-row[3], row[0], row[2])))
    merged['user_weekly_tokens'] = denominator
    merged['background_weekly_tokens'] = background_week
    merged['user_weekly_conversations'] = len(weekly)
    if args.summary_output:
        args.summary_output.parent.mkdir(parents=True, exist_ok=True)
        args.summary_output.write_text(json.dumps(merged,ensure_ascii=False),encoding='utf-8')
    print(json.dumps({'report':str(output),'csv':str(csv_path),'daily_csv':str(daily_csv_path),'sources':coverage,'user_weekly_tokens':denominator,'user_weekly_conversations':len(weekly),'background_weekly_tokens':background_week,'cross_source_duplicates':merged['cross_source_duplicates'],'period_totals':reconciliation,'display_total_tokens':merged['display_total_tokens'],'display_source':merged['display_source']},ensure_ascii=False))
    return 3 if any(x['status']!='ok' for x in coverage) else 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding='utf-8')
    sys.stderr.reconfigure(encoding='utf-8')
    raise SystemExit(main())
