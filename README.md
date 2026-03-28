# Activity Report

Estimate work activity from local evidence sources:

- `activity-pulse` foreground and key-activity buckets
- git commit timestamps
- Codex session logs under `~/.codex`
- Claude Code session logs under `~/.claude`
- optional Slack messages via `slack-mcp-cli`

The tool fuses those sources into work sessions, prints daily totals, and can
show session-level detail.

## Agent Setup

If an AI agent is setting this up for the current user, it should read
[`AGENTS.md`](./AGENTS.md) first. The important part is that config must be
personalized rather than guessed:

- derive git author defaults from the current user's global git config
- ask which evidence sources to include
- ask before enabling Slack and before saving a Slack query
- ask which non-work apps should be excluded

## Quick Install

For humans and agents:

```bash
git clone https://github.com/funtusov/activity-report.git
cd activity-report
./install.sh
mkdir -p ~/.config/activity-report
activity-report sample-config > ~/.config/activity-report/config.toml
activity-report --help
```

## Requirements

- Python 3.10+
- `uv`

## Config

Default config path:

```bash
~/.config/activity-report/config.toml
```

Print a starter config:

```bash
activity-report sample-config
```

If `[git].author_names` and `[git].author_emails` are left empty, the tool falls
back to `git config --global user.name` and `git config --global user.email`.

Slack is disabled by default. Enable it only if you want Slack evidence in the
estimate.

Past-day Slack results are cached under `~/.cache/activity-report` by default,
so rerunning historical reports does not keep hitting the Slack API. The cache
is keyed by the Slack query and config, and the current day stays live.

Codex and Claude JSONL session logs also use a file-span cache under the same
cache root. Unchanged session files are parsed once and then reused across
reruns, which cuts repeated full-history scans substantially.

## Usage

Overview for a date range:

```bash
activity-report overview --since 2026-03-25 --until 2026-03-27
```

Show session detail and JSON output:

```bash
activity-report overview --since 2026-03-25 --until 2026-03-27 --show-sessions
activity-report overview --since 2026-03-25 --until 2026-03-27 --json
```

Override the Slack query for one run:

```bash
activity-report overview \
  --since 2026-03-25 \
  --until 2026-03-27 \
  --slack-query "from:your.handle"
```

Bypass or rebuild caches for one run:

```bash
activity-report overview --since 2026-03-25 --until 2026-03-27 --no-cache
activity-report overview --since 2026-03-25 --until 2026-03-27 --refresh-cache
```

Skip pulse buckets for one run:

```bash
activity-report overview --since 2026-03-25 --until 2026-03-27 --skip-pulse
```

## Model

- Codex and Claude logs contribute observed sub-session intervals by splitting
  each local session file on idle gaps.
- `activity-pulse` contributes observed per-bucket intervals whenever
  `key_down_count > 0`, and it can also contribute foreground-only buckets such
  as reading in `Preview` or `Zotero`, plus microphone-backed dictation time.
- Git commits and Slack messages contribute point events.
- Nearby evidence is merged with `session_gap_min`, which defaults to `15 min`.
- Codex and Claude evidence is additionally split on `ai_max_event_gap_min`, which
  also defaults to `15 min`, so long quiet stretches inside AI logs stop counting
  as continuous work.
- You can exclude obvious non-work apps via `[pulse].non_work_app_names` or
  `[pulse].non_work_bundle_ids` in the config.
- If a merged session starts with a point event instead of an observed interval,
  the tool adds start padding using a configurable prior.
