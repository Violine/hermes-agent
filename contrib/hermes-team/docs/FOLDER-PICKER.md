# Server-side folder picker

The team dashboard can browse directories that the Hermes service account is permitted to see. This is not a file browser on the owner's laptop. The operator must configure a safe workspace root and ensure the backend rejects paths outside the allowed roots. Confirm the selected full path before saving a project or board.

The picker should disable confirmation while a directory is loading or when the server returns an error. Cancel and Escape close the picker without changing the previous field value. The selected directory is only applied after an explicit confirmation.

## Compatibility notes

This UI is part of the team dashboard overlay and depends on the corresponding backend browsing API. The current self-hosted implementation uses an installation-specific root. Before deploying elsewhere, configure the root for the target service account and test path traversal, symlinks, unreadable directories, nonexistent paths, and sensitive-file exposure.

## Historical checks

A prior self-hosted browser session checked directory navigation, selecting a workspace, cancellation, and persistence of the confirmed path. These checks are historical and do not attest to a current server or another installation. Unit and UI contracts are in `../tests/`; run them from the repository instructions.
