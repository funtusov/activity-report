"""Session construction and reporting."""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import date, datetime, time, timedelta
from statistics import mean, median

from activity_report.models import (
    ActivityReport,
    ActivitySession,
    DailySummary,
    EvidenceInterval,
    OverviewOptions,
)


def build_report(
    items: list[EvidenceInterval],
    since: datetime,
    until: datetime,
    options: OverviewOptions,
) -> ActivityReport:
    filtered = [
        item
        for item in items
        if item.end >= since and item.start < until and item.end >= item.start
    ]
    clipped = [
        EvidenceInterval(
            source=item.source,
            start=max(item.start, since),
            end=min(item.end, until),
            label=item.label,
        )
        for item in filtered
    ]
    clipped.sort(key=lambda item: (item.start, item.end, item.source))
    source_items = Counter(item.source for item in clipped)
    sessions = _merge_sessions(clipped, options.session_gap_min)
    default_padding = _default_start_padding(sessions, options)
    finalized = [_finalize_session(session, default_padding, options) for session in sessions]
    lower_bound = sum(session.lower_bound_min for session in finalized)
    estimated = sum(session.duration_min for session in finalized)
    daily = _daily_summaries(finalized, since, until)
    return ActivityReport(
        since=since,
        until=until,
        source_items=dict(source_items),
        sessions=finalized,
        daily=daily,
        default_start_padding_min=default_padding,
        lower_bound_total_min=lower_bound,
        estimated_total_min=estimated,
    )


def _merge_sessions(items: list[EvidenceInterval], session_gap_min: float) -> list[ActivitySession]:
    if not items:
        return []
    sessions: list[list[EvidenceInterval]] = []
    current = [items[0]]
    current_end = items[0].end
    gap_limit = timedelta(minutes=session_gap_min)
    for item in items[1:]:
        gap = item.start - current_end
        if gap <= gap_limit:
            current.append(item)
            if item.end > current_end:
                current_end = item.end
            continue
        sessions.append(current)
        current = [item]
        current_end = item.end
    sessions.append(current)
    merged: list[ActivitySession] = []
    for group in sessions:
        merged.append(
            ActivitySession(
                items=group,
                start=group[0].start,
                end=max(item.end for item in group),
                effective_start=group[0].start,
                start_padding_min=0.0,
                duration_min=(max(item.end for item in group) - group[0].start).total_seconds()
                / 60.0,
            )
        )
    return merged


def _default_start_padding(
    sessions: list[ActivitySession],
    options: OverviewOptions,
) -> float:
    if options.start_padding_mode == "none":
        return 0.0
    if options.start_padding_mode == "fixed":
        return max(0.0, options.fixed_start_padding_min)
    first_gaps = []
    for session in sessions:
        if len(session.items) < 2:
            continue
        first = session.items[0]
        if not first.is_point:
            continue
        second = session.items[1]
        gap_min = max(0.0, (second.start - first.end).total_seconds() / 60.0)
        if gap_min > 0:
            first_gaps.append(gap_min)
    if not first_gaps:
        return max(0.0, options.fixed_start_padding_min)
    if options.start_padding_mode == "median-first":
        return median(first_gaps)
    if options.start_padding_mode == "mean-first":
        return mean(first_gaps)
    raise ValueError(f"Unsupported start padding mode: {options.start_padding_mode}")


def _finalize_session(
    session: ActivitySession,
    default_padding_min: float,
    options: OverviewOptions,
) -> ActivitySession:
    start_padding = 0.0
    first = session.items[0]
    if first.is_point and default_padding_min > 0:
        start_padding = default_padding_min
        if len(session.items) >= 2:
            first_gap_min = max(
                0.0,
                (session.items[1].start - first.end).total_seconds() / 60.0,
            )
            if first_gap_min > 0:
                start_padding = min(default_padding_min, first_gap_min)
    effective_start = session.start - timedelta(minutes=start_padding)
    duration_min = (session.end - effective_start).total_seconds() / 60.0
    return ActivitySession(
        items=session.items,
        start=session.start,
        end=session.end,
        effective_start=effective_start,
        start_padding_min=start_padding,
        duration_min=duration_min,
    )


def _daily_summaries(
    sessions: list[ActivitySession],
    since: datetime,
    until: datetime,
) -> list[DailySummary]:
    per_day_minutes: dict[date, float] = defaultdict(float)
    per_day_sessions: dict[date, int] = defaultdict(int)
    for session in sessions:
        current = session.effective_start
        end = session.end
        per_day_sessions[session.start.astimezone().date()] += 1
        while current < end:
            next_midnight = datetime.combine(
                current.astimezone().date() + timedelta(days=1),
                time.min,
                tzinfo=current.tzinfo,
            )
            segment_end = min(end, next_midnight)
            per_day_minutes[current.astimezone().date()] += (
                segment_end - current
            ).total_seconds() / 60.0
            current = segment_end
    day = since.astimezone().date()
    last_day = (until - timedelta(seconds=1)).astimezone().date()
    daily = []
    while day <= last_day:
        if day in per_day_minutes:
            daily.append(
                DailySummary(
                    day=day,
                    duration_min=per_day_minutes[day],
                    session_count=per_day_sessions.get(day, 0),
                )
            )
        day += timedelta(days=1)
    return daily
