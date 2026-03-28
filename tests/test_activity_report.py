"""Smoke tests for activity report analysis."""

from __future__ import annotations

import json
import unittest
from datetime import date, datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from activity_report.analysis import build_report
from activity_report.config import (
    ActivityConfig,
    AnalysisConfig,
    CacheConfig,
    GitConfig,
    PathsConfig,
    PulseConfig,
    SlackConfig,
)
from activity_report.models import EvidenceInterval, OverviewOptions
from activity_report.sources import collect_activity_pulse_intervals, collect_slack_points


UTC = timezone.utc


class ActivityReportTests(unittest.TestCase):
    def test_point_start_session_uses_median_first_gap_padding(self) -> None:
        items = [
            EvidenceInterval("git", datetime(2026, 3, 25, 9, 0, tzinfo=UTC), datetime(2026, 3, 25, 9, 0, tzinfo=UTC), "commit-a"),
            EvidenceInterval("git", datetime(2026, 3, 25, 9, 10, tzinfo=UTC), datetime(2026, 3, 25, 9, 10, tzinfo=UTC), "commit-b"),
            EvidenceInterval("git", datetime(2026, 3, 25, 11, 0, tzinfo=UTC), datetime(2026, 3, 25, 11, 0, tzinfo=UTC), "commit-c"),
            EvidenceInterval("git", datetime(2026, 3, 25, 11, 20, tzinfo=UTC), datetime(2026, 3, 25, 11, 20, tzinfo=UTC), "commit-d"),
        ]
        report = build_report(
            items,
            datetime(2026, 3, 25, 0, 0, tzinfo=UTC),
            datetime(2026, 3, 26, 0, 0, tzinfo=UTC),
            OverviewOptions(session_gap_min=45.0, start_padding_mode="median-first", fixed_start_padding_min=15.0),
        )
        self.assertEqual(len(report.sessions), 2)
        self.assertAlmostEqual(report.default_start_padding_min, 15.0)
        self.assertAlmostEqual(report.sessions[0].duration_min, 20.0)
        self.assertAlmostEqual(report.sessions[1].duration_min, 35.0)

    def test_observed_interval_session_gets_no_start_padding(self) -> None:
        items = [
            EvidenceInterval(
                "codex",
                datetime(2026, 3, 25, 9, 0, tzinfo=UTC),
                datetime(2026, 3, 25, 9, 40, tzinfo=UTC),
                "codex session",
            ),
            EvidenceInterval(
                "git",
                datetime(2026, 3, 25, 9, 50, tzinfo=UTC),
                datetime(2026, 3, 25, 9, 50, tzinfo=UTC),
                "commit",
            ),
        ]
        report = build_report(
            items,
            datetime(2026, 3, 25, 0, 0, tzinfo=UTC),
            datetime(2026, 3, 26, 0, 0, tzinfo=UTC),
            OverviewOptions(session_gap_min=45.0, start_padding_mode="fixed", fixed_start_padding_min=20.0),
        )
        self.assertEqual(len(report.sessions), 1)
        self.assertAlmostEqual(report.sessions[0].start_padding_min, 0.0)
        self.assertAlmostEqual(report.sessions[0].duration_min, 50.0)

    def test_activity_pulse_file_contributes_observed_bucket_intervals(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            bucket_dir = Path(tmp_dir) / "buckets"
            bucket_dir.mkdir(parents=True)
            bucket_dir.joinpath("2026-03-25.jsonl").write_text(
                "\n".join(
                    [
                        '{"app_name":"iTerm2","bucket_end":"2026-03-25T09:06:00+00:00","bucket_seconds":60,"bucket_start":"2026-03-25T09:05:00+00:00","bundle_id":"com.googlecode.iterm2","foreground_seconds":48,"microphone_active_seconds":0,"key_down_count":42,"process_id":123}',
                        '{"app_name":"Preview","bucket_end":"2026-03-25T09:08:00+00:00","bucket_seconds":60,"bucket_start":"2026-03-25T09:07:00+00:00","bundle_id":"com.apple.Preview","foreground_seconds":42,"key_down_count":0,"process_id":456}',
                        '{"app_name":"Wispr Flow","bucket_end":"2026-03-25T09:10:00+00:00","bucket_seconds":60,"bucket_start":"2026-03-25T09:09:00+00:00","bundle_id":"com.wispr.flow","foreground_seconds":0,"microphone_active_seconds":36,"key_down_count":0,"process_id":777}',
                        '{"app_name":"Music","bucket_end":"2026-03-25T09:09:00+00:00","bucket_seconds":60,"bucket_start":"2026-03-25T09:08:00+00:00","bundle_id":"com.apple.Music","foreground_seconds":55,"key_down_count":0,"process_id":789}',
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            items = collect_activity_pulse_intervals(
                Path(tmp_dir),
                datetime(2026, 3, 25, 0, 0, tzinfo=UTC),
                datetime(2026, 3, 26, 0, 0, tzinfo=UTC),
                non_work_app_names=("Music",),
            )
        self.assertEqual(len(items), 3)
        self.assertEqual(items[0].source, "pulse")
        self.assertEqual(items[0].label, "iTerm2 (com.googlecode.iterm2)")
        self.assertEqual(items[0].start, datetime(2026, 3, 25, 9, 5, tzinfo=UTC))
        self.assertEqual(items[0].end, datetime(2026, 3, 25, 9, 5, 48, tzinfo=UTC))
        self.assertEqual(items[1].label, "Preview (com.apple.Preview)")
        self.assertEqual(items[2].label, "Wispr Flow (com.wispr.flow)")

    def test_slack_points_cache_past_days(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            config = ActivityConfig(
                paths=PathsConfig(
                    development_root=Path(tmp_dir),
                    codex_home=Path(tmp_dir),
                    claude_home=Path(tmp_dir),
                    activity_pulse_home=Path(tmp_dir),
                ),
                git=GitConfig(author_names=(), author_emails=(), repo_search_depth=1),
                slack=SlackConfig(
                    enabled=True,
                    query="from:test.user",
                    cli_path="slack-mcp-cli",
                    limit_per_day=50,
                ),
                pulse=PulseConfig(
                    enabled=False,
                    include_foreground_without_keys=True,
                    min_foreground_seconds=20.0,
                    non_work_app_names=(),
                    non_work_bundle_ids=(),
                ),
                analysis=AnalysisConfig(
                    session_gap_min=45.0,
                    start_padding_mode="median-first",
                    start_padding_min=15.0,
                ),
                cache=CacheConfig(
                    enabled=True,
                    cache_dir=Path(tmp_dir) / "cache",
                ),
            )
            since = datetime(2026, 3, 25, 0, 0, tzinfo=UTC)
            until = datetime(2026, 3, 26, 0, 0, tzinfo=UTC)
            slack_output = json.dumps(
                {
                    "content": [
                        {
                            "type": "text",
                            "text": (
                                "Channel,User,Time,Text\n"
                                "proj,greg,2026-03-25T09:15:00+00:00,Standup update\n"
                            ),
                        }
                    ]
                }
            )
            with patch("activity_report.sources._local_today", return_value=date(2026, 3, 28)):
                with patch(
                    "activity_report.sources.subprocess.check_output",
                    return_value=slack_output,
                ) as mock_check_output:
                    first = collect_slack_points(config, since, until, "from:test.user")
                    second = collect_slack_points(config, since, until, "from:test.user")
                    self.assertEqual(mock_check_output.call_count, 1)
                    self.assertEqual(len(first), 1)
                    self.assertEqual(len(second), 1)
                    self.assertEqual(first[0].label, "proj: Standup update")
                    self.assertEqual(second[0].label, "proj: Standup update")
                    self.assertTrue(any(config.cache.cache_dir.rglob("2026-03-25.json")))


if __name__ == "__main__":
    unittest.main()
