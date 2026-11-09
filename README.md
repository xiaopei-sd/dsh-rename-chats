# dsh-rename-chats

A local, loopback-only bridge that lets a DSH Skill read DSH session titles and
rename them through the official `session-title` service. It does not edit
`session.v4.jsonl.zstd`, SQLite, or browser cookies.

This package is one half of a two-part tool:

- **`dsh-rename-chats` (this package)** — a DSH host plugin that exposes
  `GET /health`, `GET /sessions`, `POST /read`, and `POST /rename` on a
  process-scoped capability URL.
- **`rename-chats` Skill** — preview/confirm, batching, protection, lease,
  throttle, retry, write verification, and interruption recovery.

The split is intentional. The plugin is generic; the Skill owns the policy.

## Why a bridge

DSH's Web API is authenticated. The desktop host generates a launch token in
memory, sends it to Electron over IPC, and uses it to mint a signed cookie.
A standalone Skill script cannot obtain that token without reading credentials
or browser state. A host plugin can call `sessionQuery`, `sessionController`
and `sessionTitle` directly, in the same process that owns the sessions, without
scraping secrets. `sessionController.rename` also resumes a cold session.

The bridge never exposes the title service to the network beyond loopback. The
capability URL contains a random 192-bit path segment and is written to:

```text
$DSH_HOME/rename-chats/bridge.json   (mode 0600)
```

`DSH_HOME` defaults to `~/.dsh`. On Windows the file lives at
`C:\Users\<user>\.dsh\rename-chats\bridge.json`.

## Requirements

- DSH `0.2.0-rc.2` through `0.2.2` (the `0.2.3` line is excluded because the
  session-title contract has not been verified there).
- Node.js 22 or newer on the DSH host.
- A desktop or `dsh web` host that mounts the `webServer`, `sessions`,
  `sessionQuery`, `sessionController`, and `workspaceRegistry` services.

## Install

From npm:

```bash
dsh plugin --profile desktop add dsh-rename-chats
```

Or from this checkout:

```bash
dsh plugin --profile desktop add "D:\app\DSH workspace\DS-RSI\dsh-rename-chats"
```

Then restart the DSH desktop app (or the `dsh web` process). Check:

```bash
curl --fail --silent "$(python -c 'import json,os,pathlib; p=pathlib.Path(os.environ.get("DSH_HOME", pathlib.Path.home()/".dsh"))/"rename-chats"/"bridge.json"; print(json.loads(p.read_text())["baseUrl"] + "/health")')"
```

A healthy host returns the plugin identity and a redacted workspace list:

```json
{"ok":true,"plugin":"rename-chats","pluginVersion":"0.1.1","bridgeVersion":1,"pid":1234,"workspaces":[{"id":"492bbba8-3b91-4a43-8414-b79375ce4f1c","path":"D:\\\\app\\\\DSH workspace\\\\DS-RSI","title":"DS-RSI","sessionCount":7}]}
```

After a restart the capability URL changes; the old file is replaced. A stale
URL fails with connection refused, not with a title write.

## HTTP contract

All paths below are relative to the capability `baseUrl` from `bridge.json`.

### `GET /health`

Returns plugin and host identity. No session data.

### `GET /sessions?workspaceId=<id>&limit=40`

Lists ordinary, non-archived sessions for one workspace. Subagent rows are
excluded unless `includeSubagents=1`; archived rows are excluded unless
`includeArchived=1`. `limit` is capped at 40 to match the Skill's batch bound.

Each row:

```json
{
  "id": "session-...",
  "title": "1109｜功能｜标题整理",
  "displayTitle": "1109｜功能｜标题整理",
  "titleSource": "user",
  "titleEventSeq": 42,
  "createdAt": 1794207137483,
  "createdAtUnit": "milliseconds",
  "cwd": "D:\\app\\DSH workspace\\DS-RSI",
  "origin": "ordinary",
  "parentSession": null,
  "archived": false,
  "pinned": false,
  "live": true
}
```

`createdAt` is **milliseconds** in DSH, unlike Codex's seconds. The Skill passes
`unit: "milliseconds"` explicitly; it never infers seconds from the value.

### `POST /read`

```json
{"sessionId": "session-..."}
```

Returns the latest title snapshot, its source, the creation time, the latest
event time, and the real human messages. DSH records system context as
`user/message` too; this bridge
returns only events whose `data.source.kind` is `"user"`. It keeps the first
three and last nine human messages (up to 4,000 characters each), which is
enough to identify the task without flooding the preview.

`lastActivityAt` is the maximum numeric `time` across the session event log,
in milliseconds. Use it to select conversations active today instead of
re-reading every session on each pass.

### `POST /rename`

```json
{
  "sessionId": "session-...",
  "expectedTitle": "1109｜功能｜旧标题",
  "title": "1109｜功能｜新标题"
}
```

`expectedTitle` is mandatory. The bridge resolves the session through the same
agent resolver that the official API controller uses, reads the current title,
refuses a mismatch with `409 title-changed`, appends a `session/title` event
with `source: {kind: "user"}`, then re-folds the log and verifies that the
accepted title is visible. It returns the **normalized** title and event
sequence; callers must record the returned title, not the requested one.

The write is not a compare-and-swap. A third-party edit between the pre-check
and the append can still win. The Skill re-reads the title after the write and
records `changed_during_write` if a third title appears. This is the known
concurrency window, reduced but not eliminated.

## Security notes

- The route is registered on the host web server, which is configured to bind
  `127.0.0.1:0` by the desktop host. The handler also rejects non-loopback
  socket addresses.
- The capability URL is the credential. Do not paste it into chats, issues, or
  logs. It rotates on every host start.
- The bridge reads no cookies, environment secrets, or credentials files.
- It only renames ordinary sessions in a known workspace. Archived sessions and
  subagent descendants are rejected before the title service is called.
- Titles are normalized and may be truncated by the host's `maxTitleBytes`
  setting. The bridge returns the effective title; the Skill records it.

## Development

```bash
npm test
npm pack --dry-run
```

The unit tests cover message extraction, injected-context filtering, date unit
metadata, and ordinary/subagent/archive classification. A live host write test
is deliberately separate; run it only against a disposable session.

## License

MIT
