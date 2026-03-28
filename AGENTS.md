# Agent Setup Notes

This repo estimates likely work time from local evidence. The right setup is user-specific.

## Expected Agent Workflow

1. Install the wrapper with `./install.sh`.
2. Start from `activity-report sample-config > ~/.config/activity-report/config.toml`.
3. Read `git config --global user.name` and `git config --global user.email`.
4. If the sample config still has empty git author fields, fill them from the current user's global git config.
5. Ask the user which evidence sources should be included:
   - `activity-pulse`
   - git
   - Codex logs
   - Claude logs
   - Slack
6. Ask the user which directories should be scanned if the defaults are wrong.
7. Ask the user which obvious non-work apps should be excluded from pulse-derived evidence.
8. Ask before enabling Slack and before writing a persistent Slack query into the config.
9. Run a narrow local test range first before doing full-history scans.
10. Leave `[cache]` enabled unless the user explicitly wants live-only reruns; it prevents repeated Slack reads for past days and reuses parsed Codex/Claude session spans for unchanged JSONL files.
11. The default calibration is conservative: `[analysis].session_gap_min = 15` and `[analysis].ai_max_event_gap_min = 15`, so gaps larger than 15 minutes are treated as pauses unless the user asks for a looser model.

## Privacy Boundaries

- Do not commit or document personal email addresses, Slack handles, access tokens, or private repo paths as defaults.
- Keep the sample config generic.
- If Slack is enabled, prefer user-approved explicit queries over guessed queries.
- Cached Slack evidence for past days lives under `~/.cache/activity-report` by default. The same cache root also stores parsed Codex/Claude session spans for unchanged JSONL files. Use `--refresh-cache` after intentionally changing a persistent Slack query or other cache inputs.

## Local Verification

- `activity-report --help`
- `activity-report sample-config`
- `activity-report overview --since YYYY-MM-DD --until YYYY-MM-DD`
- `activity-report overview --since YYYY-MM-DD --until YYYY-MM-DD --json`
