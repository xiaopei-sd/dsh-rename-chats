# Changelog

## 0.1.0

- Initial local bridge for DSH session titles.
- Loopback-only capability URL written to `$DSH_HOME/rename-chats/bridge.json`.
- `GET /health`, `GET /sessions`, `POST /read`, `POST /rename`.
- Read path filters DSH injected `user/message` context by `source.kind === "user"`.
- Write path uses the official `session-title` service with an expected-title check,
  then re-folds and verifies the accepted title.
- Pair with the `rename-chats` Skill for preview, confirmation, batching,
  protection, lease, throttling, retries, and interruption recovery.
