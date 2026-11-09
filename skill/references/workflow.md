# DSH rename-chats workflow

## 阶段 0：确认通道

```powershell
python scripts/dsh_bridge.py health
```

成功返回 `ok: true`、`pluginVersion`、`bridgeVersion`、`workspaces`。失败时停止，提示安装 `dsh-rename-chats` 并重启 DSH；不要直接读 `session.v4.jsonl.zstd`、SQLite 或浏览器 cookie。

`scripts/dsh_bridge.py` 默认读取 `$DSH_HOME/rename-chats/bridge.json`；也接受 `DSH_RENAME_BRIDGE_URL`。bridge URL 带随机 token，不打印完整 URL。

## 阶段 1：选当前 workspace

从 `health.workspaces` 选择 `path` 与当前 cwd 完全相同的项；无法判断时让用户选一个。只处理该 workspace，不跨 workspace，不改归档/置顶。

```powershell
python scripts/dsh_bridge.py list --workspace-id <workspaceId> --limit 40
```

默认排除 subagent/archived；pinned 保留。每批最多 40 条。超过 40 条先取 40 条，下一批重新预览。

对每条候选执行：

```powershell
python scripts/dsh_bridge.py read --session-id <sessionId>
```

响应含 `title`、`displayTitle`、`titleSource`、`titleEventSeq`、`createdAt`（毫秒）、`messages`。`messages` 已由 bridge 过滤为 `source.kind === "user"`，并取首 3 + 末 9 条；不得把系统注入上下文当用户内容。

- `title` 为 `null` 或空串：没有可改的标题，跳过，不把 `displayTitle` 当作原名。
- `titleSource === "user"`：用户手改过，保护；除非用户明确要求解除。
- 标题已符合 `MMDD｜类型｜主题` 但与拟生成值不同：疑似手改，保护。

## 阶段 2：日期

只用 `createdAt`，不要用 `updatedAt`：

```powershell
python scripts/rename_memory.py date <createdAt> --unit milliseconds --format MMDD
```

用户明确要求 `YYMMDD` 时，把整批的 `format` 和 `date` 命令都改成 `YYMMDD`，不能混用。时区固定 `Asia/Shanghai`；不加 `--timezone` 参数。

## 阶段 3：内容裁决

按主要任务范围定类型，不要按出现的单个词定。类型只能是：

`功能`、`设计`、`修复`、`优化`、`发布`、`探索`、`文档`、`研究`

同等重要时按 `修复 → 发布 → 功能 → 优化 → 设计 → 文档 → 研究 → 探索`。主题以 12 字以内为目标；多重点时用稍宽的主题，不能漏掉主要范围。信息不足时保留原名。

## 阶段 4：prepare 与预览

写一个 UTF-8 JSON 文件（例如 `%TEMP%\rename-batch.json`）：

```json
{
  "action": "prepare",
  "caller_id": "dsh-rename-chats:<workspaceId>",
  "format": "MMDD",
  "items": [
    {
      "source": "dsh",
      "workspaceId": "<workspaceId>",
      "sessionId": "<sessionId>",
      "original_title": "<当前 title，必须是字符串>",
      "createdAt": 1794207137483,
      "unit": "milliseconds",
      "titleSource": "provider",
      "kind": "修复",
      "topic": "语音录入与提醒"
    }
  ]
}
```

```powershell
python scripts/rename_memory.py run --input "%TEMP%\rename-batch.json"
```

返回 `batch_id`、`preview_hash`、`items`（含 `status`/`reason`）。只向用户输出严格的两列表格：

| 原名称 | 新名称 |
| --- | --- |
| 原名称 A | 新名称 A |
| 跳过项 | 跳过项 |

表格之外不要写解释。用户修订标题时放弃旧预览，重新 `prepare`，不能改旧批次。

## 阶段 5：确认

```json
{
  "action": "confirm",
  "batch_id": "<batch_id>",
  "preview_hash": "<preview_hash>",
  "keys": ["<key1>", "<key2>"]
}
```

`keys` 可省略（默认全部展示项）；只能选预览里出现过的 key。未选项会被标为 `skipped/not_confirmed`。确认后只能执行这一批。

## 阶段 6：claim 与逐条执行

```json
{"action": "claim", "batch_id": "<batch_id>"}
```

返回 `token` 与 `expires_at`（10 分钟租约）。后续每个动作都带 `batch_id`、`token`、`key`。

逐条处理未结束项：

1. 用 bridge `read` 重读当前标题。
2. 调 `begin`：

```json
{
  "action": "begin",
  "batch_id": "<batch_id>",
  "token": "<token>",
  "key": "<key>",
  "current_title": "<刚复读的标题>"
}
```

- `decision:"skip"` / `"recovered"`：不写。
- `decision:"wait"`：等待 `seconds`，重读后重新 `begin`。
- `decision:"write"`：调用 bridge：

```powershell
python scripts/dsh_bridge.py rename --session-id <sessionId> --expected-title "<current_title>" --title "<begin.title>"
```

`expected-title` 必须是刚复读的标题，不是缓存值。bridge 在写前再次核对；不一致返回 `409 title-changed`，按冲突处理，不重试。

3. 写入后立即 `read` 复读，并调 `record`：

```json
{
  "action": "record",
  "batch_id": "<batch_id>",
  "token": "<token>",
  "key": "<key>",
  "current_title": "<写后复读标题>",
  "accepted_title": "<bridge 返回的实际标题>",
  "retryable": false
}
```

- 复读等于拟改值，或等于 bridge 实际返回的规范化标题：成功。
- 复读仍是原名：`failed/write_unverified`；只有网络临时故障/限流才把 `retryable` 设为 `true`，并带 `retry_after`（至少 2 秒）。
- 复读出现第三种标题：`skipped/changed_during_write` 并保护。
- `current_title` 为空：`verification_unavailable`，保持 pending，不能记成功。

4. 正常条目之间至少间隔 0.5 秒；每条最多两次写入。权限/参数/冲突错误不重试。
5. 所有项结束后：

```json
{"action": "finish", "batch_id": "<batch_id>", "token": "<token>"}
```

得到 `completed` 或 `partial`，租约释放。`partial` 可重新 `claim` 继续。

## 中断恢复

- 重新 `claim`（旧 token 过期就获取新 token）。
- 对每个未结束项先 `read`，再 `begin`。`begin` 会识别“已经写成拟改标题”并补记成功。
- 仍是原名：按重试预算继续。
- 变成第三种标题：保护跳过。
- 不要假定上一次工具返回成功；只以复读结果为准。

## 保护与解除

```json
{
  "action": "protect",
  "source": "dsh",
  "workspaceId": "<workspaceId>",
  "sessionId": "<sessionId>",
  "protected": true
}
```

解除保护时必须带刚复读的 `current_title`，并把它写为新的 `last_applied_title`。用户没有明确要求时不要解除。

## 结束与清理

`finish` 后脚本会自动 `prune`：先删超过 90 天的结束批次，再保留最近 20 批；保护记录和 `last_applied_title` 长期保留。`abandon` 仅用于放弃未完成批次；放弃前尽量核实 pending 项。

## 已知窗口

- 预览后、写入前用户可能手改标题；bridge 的写前核对会返回冲突，Skill 必须保护跳过。
- bridge 的“核对 → 写入”不是原子 CAS；写入调用本身仍有极短窗口。不要声称已彻底消除。
- DSH `maxTitleBytes` 可能截断标题；bridge 返回实际标题，`record` 用 `accepted_title` 核验，不能假定等于拟改值。
- DSH 标题事件是 last-wins 日志；写入后必须复读，不能只信 HTTP 200。
