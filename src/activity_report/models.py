"""Core models for activity evidence and reports."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime


@dataclass(frozen=True)
class EvidenceInterval:
    source: str
    start: datetime
    end: datetime
    label: str | None = None

    @property
    def is_point(self) -> bool:
        return self.start == self.end


@dataclass
class ActivitySession:
    items: list[EvidenceInterval]
    start: datetime
    end: datetime
    effective_start: datetime
    start_padding_min: float
    duration_min: float

    @property
    def lower_bound_min(self) -> float:
        return (self.end - self.start).total_seconds() / 60.0

    @property
    def sources(self) -> list[str]:
        return sorted({item.source for item in self.items})


@dataclass(frozen=True)
class DailySummary:
    day: date
    duration_min: float
    session_count: int


@dataclass
class ActivityReport:
    since: datetime
    until: datetime
    source_items: dict[str, int]
    sessions: list[ActivitySession]
    daily: list[DailySummary]
    default_start_padding_min: float
    lower_bound_total_min: float
    estimated_total_min: float

    def to_dict(self) -> dict[str, object]:
        return {
            "since": self.since.isoformat(),
            "until": self.until.isoformat(),
            "source_items": dict(sorted(self.source_items.items())),
            "default_start_padding_min": self.default_start_padding_min,
            "lower_bound_total_min": self.lower_bound_total_min,
            "estimated_total_min": self.estimated_total_min,
            "session_count": len(self.sessions),
            "daily": [
                {
                    "day": day.day.isoformat(),
                    "duration_min": day.duration_min,
                    "session_count": day.session_count,
                }
                for day in self.daily
            ],
            "sessions": [
                {
                    "start": session.start.isoformat(),
                    "end": session.end.isoformat(),
                    "effective_start": session.effective_start.isoformat(),
                    "start_padding_min": session.start_padding_min,
                    "duration_min": session.duration_min,
                    "lower_bound_min": session.lower_bound_min,
                    "sources": session.sources,
                    "item_count": len(session.items),
                    "first_label": session.items[0].label,
                    "last_label": session.items[-1].label,
                }
                for session in self.sessions
            ],
        }


@dataclass(frozen=True)
class OverviewOptions:
    session_gap_min: float
    start_padding_mode: str
    fixed_start_padding_min: float
