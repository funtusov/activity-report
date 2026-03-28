"""Collectors for git, AI logs, key buckets, and Slack evidence."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import subprocess
from datetime import date, datetime, time, timedelta
from pathlib import Path

from activity_report.config import ActivityConfig
from activity_report.models import EvidenceInterval


CACHE_SCHEMA_VERSION = 1


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
    use_cache: bool = True,
    refresh_cache: bool = False,
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
                config,
                config.paths.codex_home,
                since,
                until,
                session_gap_min=session_gap_min,
                use_cache=use_cache and config.cache.enabled,
                refresh_cache=refresh_cache,
            )
        )
    if include_claude:
        items.extend(
            collect_claude_intervals(
                config,
                config.paths.claude_home,
                since,
                until,
                session_gap_min=session_gap_min,
                use_cache=use_cache and config.cache.enabled,
                refresh_cache=refresh_cache,
            )
        )
    if include_slack:
        effective_query = slack_query or config.slack.query
        if effective_query and (config.slack.enabled or slack_query is not None):
            items.extend(
                collect_slack_points(
                    config,
                    since,
                    until,
                    effective_query,
                    use_cache=use_cache and config.cache.enabled,
                    refresh_cache=refresh_cache,
                )
            )
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
                microphone_active_seconds = (
                    _float_or_none(payload.get("microphone_active_seconds")) or 0.0
                )
                bucket_seconds = max(0.0, (end - start).total_seconds())
                observed_seconds = max(
                    foreground_seconds,
                    microphone_active_seconds,
                    bucket_seconds if foreground_seconds <= 0 and microphone_active_seconds <= 0 else 0.0,
                )
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
    config: ActivityConfig,
    codex_home: Path,
    since: datetime,
    until: datetime,
    *,
    session_gap_min: float,
    use_cache: bool = True,
    refresh_cache: bool = False,
) -> list[EvidenceInterval]:
    candidates = []
    for subdir in ("archived_sessions", "sessions"):
        root = codex_home.expanduser() / subdir
        if root.exists():
            candidates.extend(_codex_candidate_paths(root, since, until))
    return _collect_jsonl_session_intervals(
        candidates,
        source="codex",
        since=since,
        until=until,
        session_gap_min=session_gap_min,
        cache_root=_session_cache_root(config),
        use_cache=use_cache,
        refresh_cache=refresh_cache,
    )


def collect_claude_intervals(
    config: ActivityConfig,
    claude_home: Path,
    since: datetime,
    until: datetime,
    *,
    session_gap_min: float,
    use_cache: bool = True,
    refresh_cache: bool = False,
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
        cache_root=_session_cache_root(config),
        use_cache=use_cache,
        refresh_cache=refresh_cache,
    )


def _collect_jsonl_session_intervals(
    paths: list[Path],
    *,
    source: str,
    since: datetime,
    until: datetime,
    session_gap_min: float,
    cache_root: Path | None,
    use_cache: bool,
    refresh_cache: bool,
) -> list[EvidenceInterval]:
    sessions: dict[str, EvidenceInterval] = {}
    for path in sorted(set(paths)):
        label, spans = _read_jsonl_spans_cached(
            path,
            source=source,
            session_gap_min=session_gap_min,
            cache_root=cache_root,
            use_cache=use_cache,
            refresh_cache=refresh_cache,
        )
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


def _codex_candidate_paths(root: Path, since: datetime, until: datetime) -> list[Path]:
    dated_candidates: list[Path] = []
    current_day = since.astimezone().date() - timedelta(days=1)
    last_day = (until - timedelta(seconds=1)).astimezone().date()
    while current_day <= last_day:
        day_root = root / f"{current_day.year:04d}" / f"{current_day.month:02d}" / f"{current_day.day:02d}"
        if day_root.exists():
            dated_candidates.extend(day_root.rglob("*.jsonl"))
        current_day += timedelta(days=1)
    if dated_candidates:
        return dated_candidates
    return list(root.rglob("*.jsonl"))


def _session_cache_root(config: ActivityConfig) -> Path:
    return config.cache.cache_dir / "session-spans"


def _read_jsonl_spans_cached(
    path: Path,
    *,
    source: str,
    session_gap_min: float,
    cache_root: Path | None,
    use_cache: bool,
    refresh_cache: bool,
) -> tuple[str | None, list[tuple[datetime, datetime]]]:
    if not use_cache or cache_root is None:
        return _read_jsonl_spans(path, session_gap_min=session_gap_min)
    cache_path = _session_cache_path(cache_root, source, path)
    stat = _safe_stat(path)
    if stat is None:
        return None, []
    if not refresh_cache:
        cached = _read_session_span_cache(cache_path, stat, session_gap_min)
        if cached is not None:
            return cached
    label, spans = _read_jsonl_spans(path, session_gap_min=session_gap_min)
    _write_session_span_cache(cache_path, stat, session_gap_min, label, spans)
    return label, spans


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


def _safe_stat(path: Path) -> os.stat_result | None:
    try:
        return path.stat()
    except OSError:
        return None


def _session_cache_path(cache_root: Path, source: str, path: Path) -> Path:
    digest = hashlib.sha256(str(path).encode("utf-8")).hexdigest()
    return cache_root / source / digest[:2] / f"{digest}.json"


def _read_session_span_cache(
    cache_path: Path,
    stat: os.stat_result,
    session_gap_min: float,
) -> tuple[str | None, list[tuple[datetime, datetime]]] | None:
    if not cache_path.exists():
        return None
    try:
        payload = json.loads(cache_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    if payload.get("schema_version") != CACHE_SCHEMA_VERSION:
        return None
    if payload.get("size") != stat.st_size or payload.get("mtime_ns") != stat.st_mtime_ns:
        return None
    if float(payload.get("session_gap_min", -1.0)) != float(session_gap_min):
        return None
    raw_spans = payload.get("spans")
    if not isinstance(raw_spans, list):
        return None
    spans: list[tuple[datetime, datetime]] = []
    for raw_span in raw_spans:
        if not isinstance(raw_span, dict):
            continue
        start = parse_iso_datetime(str(raw_span.get("start", "")))
        end = parse_iso_datetime(str(raw_span.get("end", "")))
        if start is None or end is None or end < start:
            continue
        spans.append((start, end))
    return _string_or_none(payload.get("label")), spans


def _write_session_span_cache(
    cache_path: Path,
    stat: os.stat_result,
    session_gap_min: float,
    label: str | None,
    spans: list[tuple[datetime, datetime]],
) -> None:
    payload = {
        "schema_version": CACHE_SCHEMA_VERSION,
        "size": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
        "session_gap_min": session_gap_min,
        "label": label,
        "spans": [
            {
                "start": start.isoformat(),
                "end": end.isoformat(),
            }
            for start, end in spans
        ],
    }
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = cache_path.with_name(f".{cache_path.name}.{os.getpid()}.tmp")
    try:
        temp_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        os.replace(temp_path, cache_path)
    except OSError:
        try:
            temp_path.unlink(missing_ok=True)
        except OSError:
            pass


def collect_slack_points(
    config: ActivityConfig,
    since: datetime,
    until: datetime,
    query: str,
    *,
    use_cache: bool = True,
    refresh_cache: bool = False,
) -> list[EvidenceInterval]:
    if not use_cache:
        return _collect_slack_points_live(config, since, until, query)
    local_tz = since.tzinfo or datetime.now().astimezone().tzinfo
    if local_tz is None:
        return _collect_slack_points_live(config, since, until, query)
    current_day = since.astimezone(local_tz).date()
    last_day = (until - timedelta(seconds=1)).astimezone(local_tz).date()
    today = _local_today(local_tz)
    cache_key = _slack_cache_key(config, query, local_tz)
    items: list[EvidenceInterval] = []
    missing_past_days: list[date] = []
    live_days: list[date] = []
    while current_day <= last_day:
        if current_day < today:
            cached = None
            if not refresh_cache:
                cached = _read_slack_cache_day(config, cache_key, current_day)
            if cached is None:
                missing_past_days.append(current_day)
            else:
                items.extend(_clip_intervals(cached, since, until))
        else:
            live_days.append(current_day)
        current_day += timedelta(days=1)
    for start_day, end_day in _group_contiguous_days(missing_past_days):
        range_start, range_end = _day_bounds(start_day, end_day, local_tz)
        fresh_items = _collect_slack_points_live(config, range_start, range_end, query)
        _write_slack_cache_days(config, cache_key, start_day, end_day, fresh_items, local_tz)
        items.extend(_clip_intervals(fresh_items, since, until))
    for start_day, end_day in _group_contiguous_days(live_days):
        range_start, range_end = _day_bounds(start_day, end_day, local_tz)
        items.extend(_clip_intervals(_collect_slack_points_live(config, range_start, range_end, query), since, until))
    items.sort(key=lambda item: (item.start, item.end, item.label or ""))
    return items


def _collect_slack_points_live(
    config: ActivityConfig,
    since: datetime,
    until: datetime,
    query: str,
) -> list[EvidenceInterval]:
    items: list[EvidenceInterval] = []
    local_tz = since.tzinfo or datetime.now().astimezone().tzinfo
    if local_tz is None:
        return items
    start_day = since.astimezone(local_tz).date()
    end_day = until.astimezone(local_tz).date()
    day_count = max((end_day - start_day).days, 1)
    search_query = f"{query} after:{start_day.isoformat()} before:{end_day.isoformat()}"
    args = json.dumps(
        {
            "search_query": search_query,
            "limit": config.slack.limit_per_day * day_count,
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


def _slack_cache_key(
    config: ActivityConfig,
    query: str,
    local_tz: object,
) -> str:
    payload = {
        "schema_version": CACHE_SCHEMA_VERSION,
        "query": query,
        "cli_path": config.slack.cli_path,
        "limit_per_day": config.slack.limit_per_day,
        "timezone": getattr(local_tz, "key", None) or str(local_tz),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:16]


def _slack_cache_path(config: ActivityConfig, cache_key: str, day: date) -> Path:
    return config.cache.cache_dir / "slack" / cache_key / f"{day.isoformat()}.json"


def _read_slack_cache_day(
    config: ActivityConfig,
    cache_key: str,
    day: date,
) -> list[EvidenceInterval] | None:
    path = _slack_cache_path(config, cache_key, day)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    intervals = payload.get("intervals")
    if not isinstance(intervals, list):
        return None
    items: list[EvidenceInterval] = []
    for raw_item in intervals:
        if not isinstance(raw_item, dict):
            continue
        start = parse_iso_datetime(str(raw_item.get("start", "")))
        end = parse_iso_datetime(str(raw_item.get("end", "")))
        if start is None or end is None or end < start:
            continue
        items.append(
            EvidenceInterval(
                source=str(raw_item.get("source") or "slack"),
                start=start,
                end=end,
                label=_string_or_none(raw_item.get("label")),
            )
        )
    return items


def _write_slack_cache_days(
    config: ActivityConfig,
    cache_key: str,
    start_day: date,
    end_day: date,
    items: list[EvidenceInterval],
    local_tz: object,
) -> None:
    by_day: dict[date, list[EvidenceInterval]] = {}
    for item in items:
        item_day = item.start.astimezone(local_tz).date()
        by_day.setdefault(item_day, []).append(item)
    current_day = start_day
    while current_day <= end_day:
        path = _slack_cache_path(config, cache_key, current_day)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema_version": CACHE_SCHEMA_VERSION,
            "source": "slack",
            "day": current_day.isoformat(),
            "cached_at": datetime.now().astimezone().isoformat(),
            "intervals": [
                {
                    "source": item.source,
                    "start": item.start.isoformat(),
                    "end": item.end.isoformat(),
                    "label": item.label,
                }
                for item in sorted(
                    by_day.get(current_day, []),
                    key=lambda interval: (interval.start, interval.end, interval.label or ""),
                )
            ],
        }
        temp_path = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        try:
            temp_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            os.replace(temp_path, path)
        except OSError:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass
        current_day += timedelta(days=1)


def _group_contiguous_days(days: list[date]) -> list[tuple[date, date]]:
    if not days:
        return []
    ordered = sorted(set(days))
    groups: list[tuple[date, date]] = []
    start_day = ordered[0]
    end_day = ordered[0]
    for current_day in ordered[1:]:
        if current_day == end_day + timedelta(days=1):
            end_day = current_day
            continue
        groups.append((start_day, end_day))
        start_day = current_day
        end_day = current_day
    groups.append((start_day, end_day))
    return groups


def _day_bounds(start_day: date, end_day: date, local_tz: object) -> tuple[datetime, datetime]:
    start = datetime.combine(start_day, time.min, tzinfo=local_tz)
    end = datetime.combine(end_day + timedelta(days=1), time.min, tzinfo=local_tz)
    return start, end


def _clip_intervals(
    items: list[EvidenceInterval],
    since: datetime,
    until: datetime,
) -> list[EvidenceInterval]:
    clipped: list[EvidenceInterval] = []
    for item in items:
        if item.end < since or item.start >= until:
            continue
        clipped.append(
            EvidenceInterval(
                source=item.source,
                start=max(item.start, since),
                end=min(item.end, until),
                label=item.label,
            )
        )
    return clipped


def _local_today(local_tz: object) -> date:
    return datetime.now(tz=local_tz).date()


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
