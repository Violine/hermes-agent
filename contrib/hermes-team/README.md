# Hermes Team

Hermes Team is a self-hosted workflow that coordinates a bounded set of specialist profiles through the native Hermes Kanban board. It covers intake, requirements, technical design, task planning, independent challenge and test design, implementation, review, QA, integration, trusted checks, and a final decision by the owner.

The package is under active development. The files in this contribution describe the implementation and deployment adapter; they do not by themselves mean a complete workflow is installed or has passed an end-to-end run. The upstream dashboard and core patches are tied to a specific Hermes source revision. Check [`FORK.md`](../../FORK.md) for the exact branch revision and checks run for this export, then use the compatibility and deployment notes below before installing it anywhere.

## Workflow at a glance

```text
request and attachments
  → analyst prepares SPEC
  → owner approves requirements
  → planner prepares DESIGN and PLAN
  → skeptic and test designer review in parallel
  → judge decides whether the plan is ready
  → owner approves the plan (configurable)
  → dependency-ordered implementation
  → reviewer and QA inspect the same commit
  → integrator checks evidence and trusted regressions
  → owner accepts or requests follow-up work
```

The owner always controls requirements and final acceptance. A setting can skip the separate plan-approval step after the independent judge accepts the plan. It cannot approve requirements or accept delivery on the owner's behalf. The default execution policy is deliberately bounded; limits, retries, waits, recovery, and batch regression are described in the [user guide](docs/USER-GUIDE.md) and [execution policy](docs/execution-policy.md).

## What is included

- A workflow controller and API, with durable request state, approval revisions, intake attachments, project context, quotas, worker budgets, recovery, and verification progress.
- Thirteen specialist profiles: analyst, planner, skeptic, test designer, judge, backend, frontend, mobile, Go, reviewer, QA, integrator, and coordinator.
- A Hermes plugin for Telegram intake and owner notifications, plus a team dashboard integrated with the native Kanban board.
- Reusable templates, onboarding prompts, project configuration, safe background check queues, and an isolated test-runner adapter.
- UI source and patches for the Hermes dashboard, including Russian localization, readable Kanban status, clearer waits, and folder selection.

See [roles](roles/), [skills](skills/engineering-team/), [templates](templates/), and the [technical notes](docs/).

## Deployment and compatibility

This is a contribution bundle, not a generic one-command installer. Some scripts and service templates currently contain paths, container names, and network defaults for one Linux deployment. The dashboard patches target the Hermes revision named in [dashboard localization notes](docs/DASHBOARD-LOCALIZATION.md); apply them only after checking the base revision and resolving no conflicts. Do not copy state databases, auth files, environment files, logs, or server backups from a running instance.

A portable deployment should first parameterize the installation root, Hermes home, container/runtime, service user, board and queue paths, and dashboard origin. Follow the [operations guide](docs/OPERATIONS.md) and review every service and network assumption before installation. The optional local-network proxy is deployment-specific; dashboard authentication and network isolation must remain enabled.

## Documentation map

- [User guide](docs/USER-GUIDE.md): dashboard, roles, queues, budgets, quota behavior, recovery, and command examples.
- [Owner workflow](docs/OWNER-WORKFLOW.md): create requests, attach files, answer questions, approve requirements and plans, and accept delivery.
- [Operations](docs/OPERATIONS.md): deployment boundaries, backups, upgrades, diagnostics, and rollback principles.
- [Execution policy](docs/execution-policy.md): parallel workers, test gates, batching, and regression behavior.
- [Validation notes](docs/VALIDATION.md): historical categories of checks and their limits; this is not a current live deployment report.
- [UI and Kanban](docs/DASHBOARD-LOCALIZATION.md), [Kanban usability](docs/KANBAN-USABILITY.md), and [folder picker](docs/FOLDER-PICKER.md).
- [Quota sources](docs/QUOTA-SOURCES.md), [test-runtime prerequisites](docs/TEST-RUNTIME-PREREQUISITES.md), [document attachment checks](docs/DOCUMENT-ATTACHMENTS-VALIDATION.md), and [owner-question checks](docs/OWNER-QUESTIONS-VALIDATION.md).
- [Project onboarding prompt](docs/PROJECT-ONBOARDING-PROMPT.md): prepare a reviewable context manifest for a target repository.

## Development

Run tests from the Hermes source checkout using its supported test runner. Team-specific unit and UI tests live under `contrib/hermes-team/tests/`; integration checks that require containers or a live host need a separately provisioned, isolated environment. Do not run scripts in this package against a shared production database.

This fork is for review and continued development. Merge, release, installation on a shared server, and production rollout are separate owner decisions.
