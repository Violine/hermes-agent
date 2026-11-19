# Optional LAN dashboard proxy

This repository contains a LAN proxy adapter for a particular self-hosted Hermes installation. Its source currently uses deployment-specific bind addresses, service/container assumptions, and paths. Treat it as an example to audit and parameterize; it is not a portable default and should not be exposed to an untrusted network.

The proxy is designed to leave Hermes dashboard authentication enabled, restrict peers and Host/Origin values, avoid forwarding browser authorization headers, and keep upstream dashboard cookies server-side. Preserve those properties if adapting it. Store any credentials outside the repository in a file readable only by the service account.

Before enabling a proxy, review the target network boundary, TLS termination, same-origin mutation checks, websocket behavior, authentication lifecycle, logs, and service permissions. The self-hosted checks recorded during development are historical; they do not attest to a current deployment.
