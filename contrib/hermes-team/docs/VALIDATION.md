# Validation notes

This is a historical summary of test categories used while developing Hermes Team on one self-hosted Hermes deployment. It is not a current live end-to-end report. Exact check results for this fork export, when available, belong in the repository-level `FORK.md`; a successful unit test or an installed profile does not prove a complete live workflow.

## Covered behavior

The project test suite exercises the controller state machine, approval revision gates, owner questions, Telegram and dashboard intake, attachments, project context, quota admission, bounded worker budgets, interruption and recovery, native Kanban status projection, isolated check queues, and execution policy. Handwritten dashboard tests also cover status wording, approval state, progress summaries, and file selection. Use `scripts/run_tests.sh` from the Hermes source checkout for Python tests and the commands documented in the Kanban UI notes for JavaScript tests.

The full-workflow test uses deterministic role responses and temporary Git repositories. It exercises real local commits/worktrees and configured check commands, while replacing model calls and the live board with test doubles. Treat it as controller integration coverage, not proof of a live multi-model run.

## UI and host checks

The dashboard source and core patches were checked against the Hermes base revision named in `DASHBOARD-LOCALIZATION.md`. Browser-level checks have covered the Russian dashboard, theme behavior, readable board states, and operator-facing wait reasons on a self-hosted instance. These observations describe that historical deployment and may not apply to another Hermes revision or configuration.

Isolated backend-test tooling was exercised with a dedicated offline runtime. It returns real exit status and bounded test evidence, and it is designed to fail closed when isolation assumptions are not met. Its host service templates and paths are deployment-specific and require adaptation before reuse.

## Limits of the evidence

- A mock or temporary-board test cannot confirm provider authentication, a live Telegram bot, browser access, or an end-to-end owner approval cycle.
- A narrow integration filter is not a full integration suite. Report the exact command, source revision, and executed test count for each run.
- A green regression batch proves only the tasks and source revision it covered; it does not by itself mean the whole request is complete.
- Dashboard assets can be newer or older than their editable sources. Build or compare the shipped assets when validating a change.
- Server-specific examples in the other notes are reference defaults for one deployment, not credentials or portable installer settings.
