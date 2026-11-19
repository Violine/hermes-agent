# Demo project rules
Go standard library only. Domain rules belong in internal/tasklist, HTTP adapters in internal/httpapi.
Keep API contracts explicit. Use gofmt, table-driven tests where inputs vary, real assertions for error paths.
Baseline health response must remain compatible. Tests must not mutate tracked files or require public services.
Each task is a local commit. Use existing Git identity. Never push or alter main.
