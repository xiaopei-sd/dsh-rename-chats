---
name: rename-chats
description: 批量整理 DSH 当前 workspace 的聊天标题。先展示“原名称/新名称”两列表格，用户确认后才逐条写入；保护用户手改标题，默认每批不超过 40 条。
whenToUse: 用户要求整理、批量改名、清理或统一 DSH 聊天标题时。
disable-model-invocation: false
user-invocable: true
---

# rename-chats

把 DSH 当前 workspace 的聊天标题整理成“日期｜类型｜主题”。只改标题，不改归档、置顶、项目归属、消息内容或会话顺序。

## 硬约束

- 只处理**当前 workspace**的普通会话；默认排除 subagent 与 archived，pinned 保留。
- 每批最多 40 条；超过时分批预览、分批确认。
- 预览只能是严格的两列表格：

  | 原名称 | 新名称 |
  | --- | --- |

  跳过项两列写同一个名称；表格之外不写解释。
- 日期只用会话的 `createdAt`，按 `Asia/Shanghai` 转成 `MMDD`。用户明确选择 `YYMMDD` 时，整批都用 `YYMMDD`，不能混用。
- 默认格式为 `MMDD｜类型｜主题`；`｜` 为全角竖线。
- 类型只能是：功能、设计、修复、优化、发布、探索、文档、研究。
- 主题以 12 个汉字以内为目标；宁可稍宽，不要漏掉主要任务范围。多重点难以概括时用更宽的主题。
- 类型按内容裁决；同等重要时按：修复 → 发布 → 功能 → 优化 → 设计 → 文档 → 研究 → 探索。
- 信息不足、标题为空、无法判断类型或主题时，保留原名并标为跳过。
- 用户手改过的标题必须保护：`titleSource` 为 `user`，或上次设置值与当前标题不一致，或首次就与拟生成值不同且已符合格式。保护持续到用户明确解除，不能因为再次预览而覆盖。
- 只允许通过本 Skill 的 bridge 读写标题；禁止直接改 DSH 数据库、`session.v4.jsonl.zstd`、cookie、凭据或 Chromium 存储。

## 操作顺序

1. 先读 `references/workflow.md` 的对应阶段。
2. 执行 `python scripts/dsh_bridge.py health`；不可用就停止，提示用户安装/重启 DSH bridge，不得绕过。
3. 列出当前 workspace 的普通会话；逐条 `read` 取标题、`createdAt`、`titleSource` 和真实人类消息。
4. 用 `scripts/rename_memory.py` 的 `prepare` 生成本批；只展示两列表格。
5. 用户确认后，用同一 `batch_id` 与 `preview_hash` 执行 `confirm → claim → begin → bridge rename → record → finish`。
6. 完成后只报告成功、跳过、失败、未核实数量，不粘贴过程日志。

## 执行纪律

- 写前必须重新读当前标题，`begin` 返回 `decision:"write"` 才调用 bridge。
- 每次写入后重新 `read` 核验；以实际返回/复读的标题为准，不假定等于拟改值。
- 正常条目至少间隔 0.5 秒；每条最多两次写入。
- 只对网络临时故障/限流重试；权限、参数、冲突（如 `title-changed`）不重试。
- 中断后重新 `claim`，逐条复读再 `begin`；已是拟改标题则补记成功，仍是原名按预算恢复，出现第三种标题则保护跳过。
- 写入和预览之间仍存在外部手改窗口；无法原子比较。发现标题已变时保护并跳过，不要覆盖。
- 每次成功更新后清理已结束批次：先删超过 90 天，再保留最近 20 批；保护记录与 `last_applied_title` 长期保留。
- 状态文件默认 `$DSH_HOME/rename-chats/state.json`，未设置 `DSH_HOME` 时用 `~/.dsh/rename-chats/state.json`。不要读写 Codex 的 `~/.codex/rename-chats/state.json`。
- 只保存标题、ID、日期与执行元数据；不保存消息正文。

## 安装与兼容

- Skill 放在 `~/.dsh/skills/rename-chats/`；DSH 只读 `SKILL.md` frontmatter，不读取 `agents/openai.yaml`。
- bridge 插件负责 `GET /health`、`GET /sessions`、`POST /read`、`POST /rename`，并在 `$DSH_HOME/rename-chats/bridge.json` 写入带随机 token 的本地 URL。
- 如果 `health` 失败，不要改用直接读日志或 SQLite 的替代方案；让用户先安装插件并重启 DSH。
