"""Config loading for the activity report CLI."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import subprocess
from typing import Any

try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib

DEFAULT_CONFIG_PATH = Path.home() / ".config" / "activity-report" / "config.toml"


@dataclass(frozen=True)
class PathsConfig:
    development_root: Path
    codex_home: Path
    claude_home: Path
    activity_pulse_home: Path


@dataclass(frozen=True)
class GitConfig:
    author_names: tuple[str, ...]
    author_emails: tuple[str, ...]
    repo_search_depth: int


@dataclass(frozen=True)
class SlackConfig:
    enabled: bool
    query: str | None
    cli_path: str
    limit_per_day: int


@dataclass(frozen=True)
class PulseConfig:
    enabled: bool
    include_foreground_without_keys: bool
    min_foreground_seconds: float
    non_work_app_names: tuple[str, ...]
    non_work_bundle_ids: tuple[str, ...]


@dataclass(frozen=True)
class AnalysisConfig:
    session_gap_min: float
    start_padding_mode: str
    start_padding_min: float


@dataclass(frozen=True)
class ActivityConfig:
    paths: PathsConfig
    git: GitConfig
    slack: SlackConfig
    pulse: PulseConfig
    analysis: AnalysisConfig


def _load_toml(path: Path) -> dict[str, Any]:
    expanded = path.expanduser()
    if not expanded.exists():
        return {}
    with expanded.open("rb") as fh:
        data = tomllib.load(fh)
    if not isinstance(data, dict):
        raise ValueError(f"Expected TOML table in {expanded}")
    return data


def _string_list(value: Any, default: tuple[str, ...]) -> tuple[str, ...]:
    if not isinstance(value, list):
        return default
    items = [item.strip() for item in value if isinstance(item, str) and item.strip()]
    return tuple(items) if items else default


def _string_or_none(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None


def _float_value(value: Any, default: float) -> float:
    if value in (None, ""):
        return default
    return float(value)


def _int_value(value: Any, default: int) -> int:
    if value in (None, ""):
        return default
    return int(value)


def _bool_value(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    return default


def _git_config_value(key: str) -> str | None:
    try:
        output = subprocess.check_output(
            ["git", "config", "--global", "--get", key],
            text=True,
            stderr=subprocess.DEVNULL,
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    stripped = output.strip()
    return stripped or None


def load_config(path: Path = DEFAULT_CONFIG_PATH) -> ActivityConfig:
    data = _load_toml(path)
    paths = data.get("paths") if isinstance(data.get("paths"), dict) else {}
    git = data.get("git") if isinstance(data.get("git"), dict) else {}
    slack = data.get("slack") if isinstance(data.get("slack"), dict) else {}
    pulse = data.get("pulse") if isinstance(data.get("pulse"), dict) else {}
    analysis = data.get("analysis") if isinstance(data.get("analysis"), dict) else {}
    return ActivityConfig(
        paths=PathsConfig(
            development_root=Path(
                _string_or_none(paths.get("development_root")) or "~/Development"
            ).expanduser(),
            codex_home=Path(_string_or_none(paths.get("codex_home")) or "~/.codex").expanduser(),
            claude_home=Path(_string_or_none(paths.get("claude_home")) or "~/.claude").expanduser(),
            activity_pulse_home=Path(
                _string_or_none(paths.get("activity_pulse_home"))
                or "~/Library/Application Support/activity-pulse"
            ).expanduser(),
        ),
        git=GitConfig(
            author_names=_string_list(
                git.get("author_names"),
                tuple(
                    value
                    for value in (_git_config_value("user.name"),)
                    if value is not None
                ),
            ),
            author_emails=_string_list(
                git.get("author_emails"),
                tuple(
                    value
                    for value in (_git_config_value("user.email"),)
                    if value is not None
                ),
            ),
            repo_search_depth=_int_value(git.get("repo_search_depth"), 3),
        ),
        slack=SlackConfig(
            enabled=_bool_value(slack.get("enabled"), False),
            query=_string_or_none(slack.get("query")),
            cli_path=_string_or_none(slack.get("cli_path")) or "slack-mcp-cli",
            limit_per_day=_int_value(slack.get("limit_per_day"), 500),
        ),
        pulse=PulseConfig(
            enabled=_bool_value(pulse.get("enabled"), True),
            include_foreground_without_keys=_bool_value(
                pulse.get("include_foreground_without_keys"),
                True,
            ),
            min_foreground_seconds=_float_value(pulse.get("min_foreground_seconds"), 20.0),
            non_work_app_names=_string_list(pulse.get("non_work_app_names"), ()),
            non_work_bundle_ids=_string_list(pulse.get("non_work_bundle_ids"), ()),
        ),
        analysis=AnalysisConfig(
            session_gap_min=_float_value(analysis.get("session_gap_min"), 45.0),
            start_padding_mode=_string_or_none(analysis.get("start_padding_mode"))
            or "median-first",
            start_padding_min=_float_value(analysis.get("start_padding_min"), 15.0),
        ),
    )


def sample_config_text() -> str:
    return """[paths]
development_root = "~/Development"
codex_home = "~/.codex"
claude_home = "~/.claude"
activity_pulse_home = "~/Library/Application Support/activity-pulse"

[git]
# Leave empty to fall back to `git config --global user.name` and `user.email`.
author_names = []
author_emails = []
repo_search_depth = 3

[slack]
enabled = false
query = ""
cli_path = "slack-mcp-cli"
limit_per_day = 500

[pulse]
enabled = true
include_foreground_without_keys = true
min_foreground_seconds = 20
non_work_app_names = []
non_work_bundle_ids = []

[analysis]
session_gap_min = 45
start_padding_mode = "median-first"
start_padding_min = 15
"""
