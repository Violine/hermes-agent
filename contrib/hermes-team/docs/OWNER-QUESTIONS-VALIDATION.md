# Owner-question workflow

The analyst can return a bounded `QUESTIONS.json` with one to five questions. Each question includes an identifier, clear prompt, realistic example, answer options with consequences, and an explained recommendation. Invalid data blocks workflow progression as a technical error; it is not silently treated as an owner decision.

The owner may answer through the dashboard or Telegram. Each answer is bound to the current question and request revision. Stale callbacks are rejected. Answering a question only clarifies requirements; it does not approve the requirements, plan, or delivery. After the answers arrive, the analyst updates the requirements package and submits a new revision for approval.

Tests cover validation, cross-channel answer persistence, stale-button rejection, and answer identity. The broader [validation notes](VALIDATION.md) distinguish simulated contracts from historical host checks.
