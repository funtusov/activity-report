"""CLI entrypoint for activity reporting."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, time, timedelta
from pathlib import Path

from activity_report.analysis import build_report
from activity_report.config import DEFAULT_CONFIG_PATH, load_config, sample_config_text
from activity_report.models import OverviewOptions
from activity_report.sources import collect_all_evidence


def _date_range(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"Expected YYYY-MM-DD, got {value!r}") from exc


def _format_minutes(total_min: float) -> str:
    rounded = int(round(total_min))
    hours, minutes = divmod(rounded, 60)
    return f"{hours:02d}:{minutes:02d}"


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="activity-report")
    subparsers = parser.add_subparsers(dest="command", required=True)

    overview = subparsers.add_parser("overview", help="Estimate activity for a date range")
    overview.add_argument("--since", required=True, type=_date_range, help="Start day, inclusive")
    overview.add_argument("--until", required=True, type=_date_range, help="End day, inclusive")
    overview.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    overview.add_argument("--json", action="store_true", help="Emit JSON")
    overview.add_argument(
        "--show-sessions",
        action="store_true",
        help="Show merged session detail",
    )
    overview.add_argument("--slack-query", help="Override Slack query for this run")
    overview.add_argument(
        "--no-cache",
        action="store_true",
        help="Disable evidence caches for this run",
    )
    overview.add_argument(
        "--refresh-cache",
        action="store_true",
        help="Rebuild past-day caches instead of reusing them",
    )
    overview.add_argument("--session-gap-min", type=float, help="Gap threshold in minutes")
    overview.add_argument(
        "--ai-max-event-gap-min",
        type=float,
        help="Maximum gap between Codex/Claude events before treating it as a pause",
    )
    overview.add_argument(
        "--start-padding-mode",
        choices=("median-first", "mean-first", "fixed", "none"),
        help="How to estimate missing work before a point-start session",
    )
    overview.add_argument(
        "--start-padding-min",
        type=float,
        help="Fixed start padding or fallback padding in minutes",
    )
    overview.add_argument("--skip-git", action="store_true")
    overview.add_argument("--skip-pulse", action="store_true")
    overview.add_argument("--skip-codex", action="store_true")
    overview.add_argument("--skip-claude", action="store_true")
    overview.add_argument("--skip-slack", action="store_true")

    subparsers.add_parser("config-path", help="Print the default config path")
    subparsers.add_parser("sample-config", help="Print a starter config")
    return parser


def _cmd_overview(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    options = OverviewOptions(
        session_gap_min=args.session_gap_min or config.analysis.session_gap_min,
        start_padding_mode=args.start_padding_mode or config.analysis.start_padding_mode,
        fixed_start_padding_min=(
            args.start_padding_min
            if args.start_padding_min is not None
            else config.analysis.start_padding_min
        ),
    )
    local_tz = datetime.now().astimezone().tzinfo
    since = datetime.combine(args.since, time.min, tzinfo=local_tz)
    until = datetime.combine(args.until + timedelta(days=1), time.min, tzinfo=local_tz)
    items = collect_all_evidence(
        config,
        since,
        until,
        session_gap_min=options.session_gap_min,
        ai_max_event_gap_min=(
            args.ai_max_event_gap_min
            if args.ai_max_event_gap_min is not None
            else config.analysis.ai_max_event_gap_min
        ),
        include_pulse=not args.skip_pulse,
        include_git=not args.skip_git,
        include_codex=not args.skip_codex,
        include_claude=not args.skip_claude,
        include_slack=not args.skip_slack,
        slack_query=args.slack_query,
        use_cache=not args.no_cache,
        refresh_cache=args.refresh_cache,
    )
    report = build_report(items, since, until, options)
    if args.json:
        print(json.dumps(report.to_dict(), indent=2))
        return 0
    print(
        f"Range: {args.since.isoformat()} -> {args.until.isoformat()}  "
        f"Sessions: {len(report.sessions)}"
    )
    print(
        f"Estimated total: {_format_minutes(report.estimated_total_min)}  "
        f"Lower bound: {_format_minutes(report.lower_bound_total_min)}"
    )
    print(
        f"Start padding: {report.default_start_padding_min:.1f} min  "
        f"Gap threshold: {options.session_gap_min:.1f} min"
    )
    if report.source_items:
        sources = ", ".join(
            f"{source}={count}" for source, count in sorted(report.source_items.items())
        )
        print(f"Source items: {sources}")
    if report.daily:
        print("")
        print("Daily")
        for day in report.daily:
            print(
                f"{day.day.isoformat()}  {_format_minutes(day.duration_min)}  "
                f"sessions={day.session_count}"
            )
    if args.show_sessions and report.sessions:
        print("")
        print("Sessions")
        for session in report.sessions:
            sources = ",".join(session.sources)
            first_label = session.items[0].label or "-"
            last_label = session.items[-1].label or "-"
            print(
                f"{session.effective_start.astimezone().isoformat()} -> "
                f"{session.end.astimezone().isoformat()}  "
                f"{_format_minutes(session.duration_min)}  "
                f"padding={session.start_padding_min:.1f}m  "
                f"sources={sources}"
            )
            print(f"  first: {first_label}")
            if last_label != first_label:
                print(f"  last:  {last_label}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.command == "config-path":
        print(DEFAULT_CONFIG_PATH)
        return 0
    if args.command == "sample-config":
        print(sample_config_text().rstrip("\n"))
        return 0
    if args.command == "overview":
        return _cmd_overview(args)
    parser.error(f"Unsupported command: {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
