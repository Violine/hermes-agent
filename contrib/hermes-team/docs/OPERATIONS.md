# Operations and deployment

Hermes Team is a controller and plugin layered on the Hermes runtime and native Kanban board. The current scripts and service templates are adapted to one self-hosted Linux deployment. They contain deployment-specific account, container, filesystem, and network assumptions; inspect and parameterize those before using them on another host. This bundle is not a generic installer.

## Components

- `team_engine/` stores requests, approvals, role dispatch, project context, quota decisions, budgets, verification and recovery state.
- `plugins/engineering-team/` integrates Telegram intake and owner notifications with the Hermes plugin lifecycle.
- `dashboard/` provides the operator UI, status projection, theme and companion endpoints. Native Kanban remains the source for worker cards and runs.
- `scripts/` contains CLI wrappers, queue bridges, isolated test runners and example user-service units. Some helpers are also deployed outside the runtime tree; use the service unit's actual `ExecStart` to identify them.

## Deployment boundaries

1. Pin the Hermes base revision and check every core/dashboard patch against that exact source before applying it. Refuse conflicts; do not force-apply a stale patch.
2. Parameterize the service user, Hermes home, source root, container, queue/data paths, toolchain, allowed workspace roots, and dashboard origin. Use restrictive ownership and file permissions for state.
3. Keep model providers and Hermes within the host's approved network boundary. A test runner that must be offline should use its own rootless daemon and namespaces. Do not weaken host firewall, VPN, or routing rules to make a test pass.
4. Keep tokens, auth databases, environment files, request state, native Kanban databases, worktrees, logs, private reports, and backups outside the source repository. Never include live secrets or owner data in a public patch.
5. Back up target-specific configuration and generated assets before installation. Store the backup outside the active source tree, record its checksum and scope, and test restoration in a temporary directory before relying on it.

## Changes and upgrades

For code changes, test the controller and plugin offline, then verify the native Kanban and dashboard contracts. For Hermes upgrades, compare upstream source locations and APIs, rebuild generated UI assets from their editable sources, and re-run relevant checks against the new revision. Installing a copied Python module without its matching core patches, plugin assets, role files, external worker tools, and service definitions can leave the workflow partially functional.

Apply updates during a quiet period. Preserve the existing state database and queue data. Restart only the components whose code changed and only after their active work has finished. Confirm service health, controller status, queue workers, dashboard authentication, board access, and a bounded non-production smoke request. Do not declare an end-to-end workflow healthy based only on service state or installed profiles.

## Diagnosis and recovery

- Compare the exact request revision, branch/SHA, board task, native run, queue job, and test report before retrying. Avoid duplicate heavyweight checks for the same SHA.
- Separate provider/quota waits, owner-input gates, test waits, environment errors, and implementation failures. A wait is healthy only when the underlying job or gate can be identified.
- Resume an interrupted check through its supported queue/CLI interface. Do not mark unrun tests as passed or edit the candidate snapshot while a check is consuming it.
- Restore code/assets from a verified backup in a temporary location first. Do not restore configuration, state databases, queues, credentials, VPN, or unrelated application data as part of a code rollback.

## Audit and privacy

Before publishing, review `git status`, staged files, commit history, generated archives, documentation, and screenshots. Exclude service credentials, absolute private paths when unnecessary, host identifiers, request/task/run IDs, private project SHAs, runtime state, evidence logs, and customer data. Keep a sanitized historical validation summary separate from current branch CI results.
