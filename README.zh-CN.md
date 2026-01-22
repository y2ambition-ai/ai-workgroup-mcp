# Bridge MCP v4 — AI 工作组聊天（本地优先）

一个极简的 MCP 服务器，将单个 SQLite 文件转变为持久化消息总线，支持多个 AI 代理/终端之间通信。

**仅 3 个工具：**
- `get_status()` — 显示你的 ID 和在线的代理
- `send(to, content)` — 私信 / 多播 / `all` 广播给在线代理
- `recv(wait_seconds=86400)` — 接收消息（虚拟阻塞）

## 快速开始
```bash
pip install mcp
claude mcp add bridge "python" "C:/ccbridge/bridge.py"
```

## 说明
- `send("all", ...)` 向当前所有在线代理广播（不包括你自己）。
- 专为单机 / 受控的本地环境设计。

## 数据库路径（默认 C 盘，可自行修改）
- 默认：`C:\mcp_msg_pool\bridge_v4.db`
- 兜底：如果默认目录不可写，会自动使用 `C:\Users\Public\mcp_msg_pool\bridge_v4.db`
- 如需改路径：编辑 `bridge.py` 顶部的 `PREFERRED_ROOT` / `FALLBACK_ROOT` 常量即可。

## 安装范围（按 Claude 规范）
MCP 注册一般分两种：
- 全局安装（对所有项目生效）
- 项目级安装（仅当前项目生效）
具体以 Claude Code/客户端的 MCP 配置方式为准。

## 没有常驻服务器
Bridge MCP 不是一直在后台跑的服务。
Claude 在 → MCP server 会被启动并运行；Claude 不在 → 进程停止。
但消息保存在 SQLite 里，所以下次启动 Claude 还能继续接收/追问/复盘。

## 消息语义
- 私信是"阅后即焚"：对方 `recv()` 读到后会删除。
- 群发/广播通过游标（state 表）去重，不会重复展示。

## 设计与性能说明（基础版）

### 设计逻辑
- **本地优先 / 零部署**：一个 MCP + 一个 SQLite 文件，就是一个"持久化消息总线"。
- **DB 即扩展接口（DB-as-API）**：外置脚本可以直接往同一个 SQLite 写消息/群发，而不用新增 MCP 工具。
- **工具面极小（只有 3 个）**：更不容易选错工具、提示词更短、更适合长期迭代与追问复盘。
- **没有常驻服务**：不是后台常驻服务器。Claude 启动它就运行，Claude 退出就停止；但消息一直在 SQLite 里。

### Token 消耗（"低消耗"的准确含义）
工具调用仍会产生输入/输出 token，但因为**只有 3 个工具**、输出格式稳定且紧凑，
模型不需要记一堆 API/指令，整体提示词膨胀更小、误调用更少，长期协作更省。

### 响应时间（基于 `bridge.py` 默认常量）
这些是由默认常量推出来的"预期范围"：
- **取消等待的响应**：≤ `RECV_TICK`（默认 `0.25s`）——`recv()` 对新指令的反应速度。
- **监听状态下收到新消息**：通常 ≤ `RECV_DB_POLL_EVERY + RECV_TICK`（默认 `2.0s + 0.25s ≈ 2.25s`）。
- **维护频率**：每 `RECV_MAINT_EVERY`（默认 `10s`）做心跳/清理。
- **分批输出**：超过 `MAX_BATCH_SIZE`（默认 `5000` 字符）会自动分批返回。

真实体验与磁盘速度、并发 agent 数、以及 Claude 客户端是否支持并发工具调用有关。

### 调参入口（最安全、最常用）
直接改 `bridge.py` 这些常量即可：
- `RECV_TICK`（取消更灵敏但更频繁唤醒）
- `RECV_DB_POLL_EVERY`（更低延迟但更频繁查库）
- `RECV_MAINT_EVERY`（心跳/清理频率）
- TTL：`HEARTBEAT_TTL` / `BROADCAST_TTL` / `DIRECT_MSG_TTL`

### 调试清单
- **确认 DB 路径**：按上面的"数据库路径"检查文件是否存在。
- **看日志**：Bridge 会在 stderr 打印 `[BRIDGE] ...` 日志；调试时可手动运行 `python bridge.py` 看启动/错误信息。
- **遇到 `DB Busy/Locked`**：
  - 关闭占用 DB 的 SQLite 查看器，
  - 避免把 DB 放在同步/扫描很激进的目录（例如网盘同步目录），
  - 降低并发写入（很多 agent 同时狂发消息），
  - 保持 busy_timeout（代码已设置）。
