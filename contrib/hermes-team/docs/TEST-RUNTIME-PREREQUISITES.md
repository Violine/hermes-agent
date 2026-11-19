# Isolated backend test runtime

The background test adapter separates long-running project checks from an agent's active model session. A narrow public request identifies the workflow request, task, committed source SHA, and an allowlisted scope. The host bridge validates the candidate, creates a clean source snapshot, runs fixed project checks in a dedicated sandbox, and returns bounded logs and test evidence with the actual exit code.

The queue is durable and idempotent for a given attempt. Status and result reads are bounded. Arbitrary commands, paths, environment variables, and Docker access are not accepted from worker roles. A rerun creates a distinct attempt. A passing exit code alone is insufficient if no tests ran, tracked files changed, or cleanup failed.

## Isolation requirements

A production-quality setup should use a dedicated rootless daemon with no network uplink, a separate PID and mount namespace, a read-only source/runtime/toolchain, and an isolated writable worktree and build cache. Never mount a host Docker socket or credentials into agent-controlled tests. Use preloaded images/dependencies when the test network is disabled. Validate these boundaries on every run and fail closed when an assumption is violated.

The provided host bridge, queue worker, service templates, and sample commands currently contain deployment-specific filesystem paths and service assumptions. They require review and parameterization before another operator installs them. Install any required OS component only through the host owner, with an explicit backup and documented rollback. Do not use a shared application database or production credentials for tests.

The implementation serializes heavyweight jobs, records the command scope, source revision, exit code, and bounded JUnit/log evidence, then cleans resources created for that test. Unit tests and integration tests should remain distinguishable; a filtered integration suite is not a full suite.
