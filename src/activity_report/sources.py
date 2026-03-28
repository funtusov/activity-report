"""Collectors for git, AI logs, key buckets, and Slack evidence."""

from __future__ import annotations

import csv
import io
import json
import os
import subprocess
from datetime import date, datetime, time, timedelta
from pathlib import Path

from activity_report.config import ActivityConfig
from activity_report.models import EvidenceInterval


def collect_all_evidence(
    config: ActivityConfig,
    since: datetime,
    until: datetime,
    *,
    session_gap_min: float,
    include_pulse: bool = True,
    include_git: bool = True,
    include_codex: bool = True,
    include_claude: bool = True,
    include_slack: bool = True,
    slack_query: str | None = None,
) -> list[EvidenceInterval]:
    items: list[EvidenceInterval] = []
    if include_pulse and config.pulse.enabled:
        items.extend(
            collect_activity_pulse_intervals(
                config.paths.activity_pulse_home,
                since,
                until,
                include_foreground_without_keys=config.pulse.include_foreground_without_keys,
                min_foreground_seconds=config.pulse.min_foreground_seconds,
                non_work_app_names=config.pulse.non_work_app_names,
                non_work_bundle_ids=config.pulse.non_work_bundle_ids,
            )
        )
    if include_git:
        items.extend(collect_git_points(config, since, until))
    if include_codex:
        items.extend(
            collect_codex_intervals(
                config.paths.codex_home,
                since,
                until,
                session_gap_min=session_gap_min,
            )
        )
    if include_claude:
        items.extend(
            collect_claude_intervals(
                config.paths.claude_home,
                since,
                until,
                session_gap_min=session_gap_min,
            )
        )
    if include_slack:
        effective_query = slack_query or config.slack.query
        if effective_query and (config.slack.enabled or slack_query is not None):
            items.extend(collect_slack_points(config, since, until, effective_query))
    return items


def collect_activity_pulse_intervals(
    activity_pulse_home: Path,
    since: datetime,
    until: datetime,
    *,
    include_foreground_without_keys: bool = True,
    min_foreground_seconds: float = 20.0,
    non_work_app_names: tuple[str, ...] = (),
    non_work_bundle_ids: tuple[str, ...] = (),
) -> list[EvidenceInterval]:
    bucket_dir = activity_pulse_home.expanduser() / "buckets"
    if not bucket_dir.exists():
        return []
    items: list[EvidenceInterval] = []
    skipped_app_names = {name.strip().casefold() for name in non_work_app_names if name.strip()}
    skipped_bundle_ids = {bundle.strip().casefold() for bundle in non_work_bundle_ids if bundle.strip()}
    current_day = since.astimezone().date()
    last_day = (until - timedelta(seconds=1)).astimezone().date()
    while current_day <= last_day:
        path = bucket_dir / f"{current_day.isoformat()}.jsonl"
        if path.exists():
            items.extend(
                _read_activity_pulse_file(
                    path,
                    since,
                    until,
                    include_foreground_without_keys=include_foreground_without_keys,
                    min_foreground_seconds=min_foreground_seconds,
                    skipped_app_names=skipped_app_names,
                    skipped_bundle_ids=skipped_bundle_ids,
                )
            )
        current_day += timedelta(days=1)
    items.sort(key=lambda item: (item.start, item.end, item.label or ""))
    return items


def _read_activity_pulse_file(
    path: Path,
    since: datetime,
    until: datetime,
    *,
    include_foreground_without_keys: bool,
    min_foreground_seconds: float,
    skipped_app_names: set[str],
    skipped_bundle_ids: set[str],
) -> list[EvidenceInterval]:
    items: list[EvidenceInterval] = []
    try:
        with path.open("r", encoding="utf-8") as fh:
            for raw_line in fh:
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(payload, dict):
                    continue
                key_down_count = payload.get("key_down_count")
                if not isinstance(key_down_count, int) or key_down_count <= 0:
                    key_down_count = 0
                start = parse_iso_datetime(str(payload.get("bucket_start", "")))
                end = parse_iso_datetime(str(payload.get("bucket_end", "")))
                if start is None or end is None or end <= since or start >= until or end < start:
                    continue
                app_name = _string_or_none(payload.get("app_name")) or "unknown app"
                bundle_id = _string_or_none(payload.get("bundle_id"))
                if app_name.casefold() in skipped_app_names:
                    continue
                if bundle_id and bundle_id.casefold() in skipped_bundle_ids:
                    continue
                foreground_seconds = _float_or_none(payload.get("foreground_seconds")) or 0.0
                bucket_seconds = max(0.0, (end - start).total_seconds())
                observed_seconds = foreground_seconds if foreground_seconds > 0 else bucket_seconds
                if key_down_count <= 0:
                    if not include_foreground_without_keys or observed_seconds < min_foreground_seconds:
                        continue
                if observed_seconds <= 0:
                    continue
                observed_end = start + timedelta(seconds=min(observed_seconds, bucket_seconds or observed_seconds))
                label = app_name if not bundle_id or bundle_id == "unknown" else f"{app_name} ({bundle_id})"
                items.append(
                    EvidenceInterval(
                        source="pulse",
                        start=max(start, since),
                        end=min(observed_end, until),
                        label=label,
                    )
                )
    except OSError:
        return []
    return items


def collect_git_points(
    config: ActivityConfig,
    since: datetime,
    until: datetime,
) -> list[EvidenceInterval]:
    repos = discover_git_repos(config.paths.development_root, config.git.repo_search_depth)
    seen_hashes: set[str] = set()
    items: list[EvidenceInterval] = []
    authors = set(config.git.author_names) | set(config.git.author_emails)
    for repo in repos:
        try:
            output = subprocess.check_output(
                [
                    "git",
                    "-C",
                    str(repo),
                    "log",
                    "--all",
                    f"--since={since.isoformat()}",
                    f"--until={until.isoformat()}",
                    "--format=%H%x09%aI%x09%an%x09%ae%x09%s",
                ],
                text=True,
                stderr=subprocess.DEVNULL,
            )
        except subprocess.CalledProcessError:
            continue
        for line in output.splitlines():
            parts = line.split("\t", 4)
            if len(parts) != 5:
                continue
            commit_hash, raw_dt, author_name, author_email, subject = parts
            if author_name not in authors and author_email not in authors:
                continue
            if commit_hash in seen_hashes:
                continue
            event_dt = parse_iso_datetime(raw_dt)
            if event_dt is None or not (since <= event_dt < until):
                continue
            seen_hashes.add(commit_hash)
            label = f"{repo.name}: {subject}" if subject else repo.name
            items.append(
                EvidenceInterval(
                    source="git",
                    start=event_dt,
                    end=event_dt,
                    label=label,
                )
            )
    return items


def discover_git_repos(root: Path, max_depth: int) -> list[Path]:
    root = root.expanduser()
    repos: list[Path] = []
    ignored_dirs = {".git", ".venv", "__pycache__", "node_modules", ".uv-cache"}
    for dirpath, dirnames, filenames in os.walk(root):
        path = Path(dirpath)
        try:
            depth = len(path.relative_to(root).parts)
        except ValueError:
            continue
        if depth > max_depth:
            dirnames[:] = []
            continue
        if ".git" in dirnames or ".git" in filenames:
            repos.append(path)
        dirnames[:] = [name for name in dirnames if name not in ignored_dirs]
    return sorted(set(repos))


def collect_codex_intervals(
    codex_home: Path,
    since: datetime,
    until: datetime,
    *,
    session_gap_min: float,
) -> list[EvidenceInterval]:
    candidates = []
    for subdir in ("archived_sessions", "sessions"):
        root = codex_home.expanduser() / subdir
        if root.exists():
            candidates.extend(root.rglob("*.jsonl"))
    return _collect_jsonl_session_intervals(
        candidates,
        source="codex",
        since=since,
        until=until,
        session_gap_min=session_gap_min,
    )


def collect_claude_intervals(
    claude_home: Path,
    since: datetime,
    until: datetime,
    *,
    session_gap_min: float,
) -> list[EvidenceInterval]:
    root = claude_home.expanduser() / "projects"
    if not root.exists():
        return []
    candidates = list(root.rglob("*.jsonl"))
    return _collect_jsonl_session_intervals(
        candidates,
        source="claude",
        since=since,
        until=until,
        session_gap_min=session_gap_min,
    )


def _collect_jsonl_session_intervals(
    paths: list[Path],
    *,
    source: str,
    since: datetime,
    until: datetime,
    session_gap_min: float,
) -> list[EvidenceInterval]:
    sessions: dict[str, EvidenceInterval] = {}
    for path in sorted(set(paths)):
        label, spans = _read_jsonl_spans(path, session_gap_min=session_gap_min)
        for index, (start, end) in enumerate(spans):
            if end < since or start >= until:
                continue
            interval = EvidenceInterval(
                source=source,
                start=max(start, since),
                end=min(end, until),
                label=label or path.stem,
            )
            key = f"{path.name}:{index}"
            sessions[key] = interval
    return list(sorted(sessions.values(), key=lambda item: (item.start, item.end, item.label or "")))


def _read_jsonl_spans(
    path: Path,
    *,
    session_gap_min: float,
) -> tuple[str | None, list[tuple[datetime, datetime]]]:
    timestamps: list[datetime] = []
    label: str | None = None
    try:
        with path.open("r", encoding="utf-8") as fh:
            for raw_line in fh:
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError:
                    continue
                event_dt = parse_timestamp_field(payload)
                if event_dt is None:
                    continue
                timestamps.append(event_dt)
                if label is None:
                    label = (
                        _string_or_none(payload.get("cwd"))
                        or _string_or_none(payload.get("sessionId"))
                        or _string_or_none(payload.get("payload", {}).get("cwd"))
                    )
    except OSError:
        return None, []
    if not timestamps:
        return label, []
    timestamps.sort()
    spans: list[tuple[datetime, datetime]] = []
    start = timestamps[0]
    end = timestamps[0]
    gap_limit = timedelta(minutes=session_gap_min)
    for event_dt in timestamps[1:]:
        if event_dt - end > gap_limit:
            spans.append((start, end))
            start = event_dt
            end = event_dt
            continue
        end = event_dt
    spans.append((start, end))
    return label, spans


def collect_slack_points(
    config: ActivityConfig,
    since: datetime,
    until: datetime,
    query: str,
) -> list[EvidenceInterval]:
    items: list[EvidenceInterval] = []
    day_after = since.astimezone().date().isoformat()
    day_before = until.astimezone().date().isoformat()
    search_query = f"{query} after:{day_after} before:{day_before}"
    args = json.dumps(
        {
            "search_query": search_query,
            "limit": config.slack.limit_per_day,
        }
    )
    try:
        output = subprocess.check_output(
            [config.slack.cli_path, "call", "conversations_search_messages", "--args", args],
            text=True,
            stderr=subprocess.DEVNULL,
        )
    except (OSError, subprocess.CalledProcessError):
        return items
    for row in _parse_slack_search_rows(output):
        event_dt = parse_iso_datetime(row.get("Time", ""))
        if event_dt is None or not (since <= event_dt < until):
            continue
        channel = row.get("Channel", "").strip()
        text = " ".join(row.get("Text", "").split())
        label = channel
        if text:
            label = f"{channel}: {text[:80]}" if channel else text[:80]
        items.append(
            EvidenceInterval(
                source="slack",
                start=event_dt,
                end=event_dt,
                label=label or "slack message",
            )
        )
    return items


def _parse_slack_search_rows(raw_output: str) -> list[dict[str, str]]:
    try:
        payload = json.loads(raw_output)
    except json.JSONDecodeError:
        return []
    csv_blob = ""
    for item in payload.get("content", []):
        if isinstance(item, dict) and item.get("type") == "text":
            csv_blob = item.get("text", "")
            break
    if not csv_blob.strip():
        return []
    reader = csv.DictReader(io.StringIO(csv_blob))
    return [dict(row) for row in reader]


def parse_timestamp_field(payload: dict[str, object]) -> datetime | None:
    value = payload.get("timestamp")
    if isinstance(value, (int, float)):
        if value > 1_000_000_000_000:
            return datetime.fromtimestamp(value / 1000.0, tz=datetime.now().astimezone().tzinfo)
        return datetime.fromtimestamp(value, tz=datetime.now().astimezone().tzinfo)
    if isinstance(value, str):
        return parse_iso_datetime(value)
    return None


def parse_iso_datetime(value: str) -> datetime | None:
    text = value.strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def _string_or_none(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None


def _float_or_none(value: object) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    if not stripped:
        return None
    try:
        return float(stripped)
    except ValueError:
        return None
