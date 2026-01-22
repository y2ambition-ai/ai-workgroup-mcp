# Bridge MCP v4 — AI 工作组聊天（本地优先）

一个极简的 MCP 服务器，将单个 SQLite 文件转变为持久化消息总线，支持多个 AI 代理/终端之间通信。

**仅 3 个工具：**
- `get_status()` — 显示你的 ID 和在线的代理
- `send(to, content)` — 私信 / 多播 / `all` 广播给在线代理
- `recv(wait_seconds=86400)` — 接收消息（虚拟阻塞）

## 为什么是 Bridge MCP（产品优势）

Bridge MCP 的设计逻辑是"Agent 24 小时待命听指挥"：
Agent 可以通过 `recv(86400)` 长时间保持监听，像工作群一样随时接收指令与汇报，而不是"发邮件等回复"。

### 你会得到什么
- **工作群式通信，而不是邮箱式**：只要 Agent 在监听，就能近实时收到消息（不是"发一封信等对方查收"）。
- **部署极轻量**：基本就是 **一个 MCP + 一个 SQLite 文件**（不需要 Redis、不需要额外 Web 服务）。
- **单文件友好**：核心就是一个 Python 脚本，易复制、易版本管理、易迁移。
- **AI 学习成本≈0**：只需要 3 个工具，输出格式稳定 → 提示词更短、误调用更少。
- **响应很快（由配置常量决定）**：
  - 取消等待响应 ≤ `RECV_TICK`（默认 `0.25s`）
  - 监听状态下收到消息通常 ≤ `RECV_DB_POLL_EVERY + RECV_TICK`（默认约 `2.25s`）
- **稳定性取向**：消息持久化在 SQLite；不是常驻服务；并通过 busy_timeout、WAL、缩短写事务窗口等方式降低锁竞争。
- **扩展性强（DB 即 API）**：外置脚本/插件可直接写入 SQLite 注入数据/事件/广播，无需新增 MCP 工具。
- **上下线自适应**：心跳维护在线列表，`send("all", ...)` 作用于当前在线快照。
- **输出可读**：时间戳 + 分组 + 分批，保持输出紧凑易读。

**说明：** 这里的"实时"指轮询式近实时，延迟由轮询间隔等配置与环境决定。

### 语义自适应接口（概念）
Bridge MCP 是轻量稳定的工作群通信底座。若要"语义自适应分发"（任务交给最合适的 Agent），需要你写一个外置 Python producer：读取外部数据→决定发给谁→写入 SQLite（群发/私聊）。

## 三终端验收流程（推荐上手方式）

1）安装：
```bash
pip install mcp
```

2）打开 3 个终端 / 3 个 Agent，每个都注册 Bridge MCP（推荐全局安装）：
```bash
claude mcp add bridge --scope=user "python" "C:/ccbridge/bridge.py"
```

3）在 1号 Agent 和 2号 Agent 里先下达一句话：
> "你保持持续监听；收到任何信息都立刻回复；回复完立刻恢复监听。"

然后执行：
```bash
recv(86400)
```

4）在 3号 Agent（主管）做群聊测试：
```bash
send("all", "测试：所有人回复自己的ID+一句当前状态，然后继续监听。")
recv(30)
```
如果没人回复，就再 `recv(30)` 收一次（部分客户端是轮询近实时）。

5）主管→员工私聊验收（建议做一次）：

**先 `get_status()` 看在线列表，用实际 ID 发送私聊。**

主管分别私聊两位员工：
```bash
send("<员工1ID>", "把你的一句当前状态汇报给我，然后继续监听。")
send("<员工2ID>", "把你的一句当前状态汇报给我，然后继续监听。")
```
主管 `recv(60)` 收汇报。

一切正常的话——恭喜你，你的 AI 团队诞生了。

## 推荐全局安装（user scope）

因为 Bridge MCP 极轻量（3 个工具、输出紧凑稳定、token 开销很低），通常更建议全局注册：
```bash
claude mcp add bridge --scope=user "python" "C:/ccbridge/bridge.py"
```
这样所有项目都能直接使用；如需项目级注册，请按 Claude 客户端的 MCP 配置规范来。

## 没有常驻服务器

Bridge MCP 不是后台常驻服务器。
Claude 在 → MCP server 会被启动并运行；Claude 不在 → 进程停止。
但消息在 SQLite 里持久化，所以跨会话不会丢。

## 消息语义

私信是"阅后即焚"：对方 `recv()` 读到后会删除。

群发/广播通过游标（state 表）去重，不会重复展示。

## 数据库路径（默认 C 盘，可自行修改）

默认：`C:\mcp_msg_pool\bridge_v4.db`

兜底：如果默认目录不可写，会自动使用 `C:\Users\Public\mcp_msg_pool\bridge_v4.db`

改路径：编辑 `bridge.py` 顶部 `PREFERRED_ROOT` / `FALLBACK_ROOT` 即可。

## 混编团队（理论支持）

理论上任何支持 MCP 的客户端都可以加入这套聊天系统。
我主要在 Claude Code/Claude Desktop 下测试；你可以尝试混编团队（Claude + GPT + Gemini 等），前提是你的工具/客户端支持 MCP。

## Token 消耗（更准确的理解）

工具调用仍会产生输入/输出 token，但因为工具面极小、输出紧凑稳定，
体验接近"让 AI 写/读落盘 TXT"，并且更利于长期复盘追问。
并发不等于更少 tokens，但能降低你的人工迭代成本：更快反馈、更好复盘、上下文更持久。

## 扩展性说明

Bridge MCP 的**通信层非常轻量**：增加更多 Agent 并不会天然"阻塞通讯"。
实际能扩到多大，主要取决于：
- **你的模型 token 预算**（人越多输出越多），
- **机器资源**（CPU/磁盘），
- 以及**存储后端能力**（SQLite 适合小中规模本地团队）。

如果你想上更大规模或更高并发，建议把 SQLite 升级到更强的后端
（例如 Postgres/Redis/消息队列），并调整轮询参数（`RECV_DB_POLL_EVERY`、`RECV_TICK`）。

## 环境要求

- Python 3.x
- `pip install mcp`
- 支持 MCP 的客户端（Claude Code / Claude Desktop 等）
- 只要你会配置 MCP server，就能接入使用

## 平台说明

目前主要在 Windows 下测试。
macOS/Linux 理论可用，但需要自行调整路径（编辑 `bridge.py` 的 `PREFERRED_ROOT` / `FALLBACK_ROOT`）。

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

---

**更多玩法示例：** [PLAYBOOK.zh-CN.md](PLAYBOOK.zh-CN.md) / [PLAYBOOK.md](PLAYBOOK.md)

*团队协作场景：建议将 [PROMPT_GLOBAL.md](PROMPT_GLOBAL.md) 复制到你的全局提示词 / CLAUDE.md 中。*

## 联系与维护说明

**联系方式：** 微信 `vvvykvvv` | 邮箱 `84927052@qq.com`

**维护说明：** 我不是程序员，也不会 GitHub；这个仓库是我个人工作流用的，基本都由 AI 操作维护。  
**有问题请优先在 GitHub Issues/Discussions 提交**，我会让 AI 定期阅读和整理。  
以上联系方式仅用于必要情况（减少垃圾信息）。

---

*Powered by Bridge MCP (by vvvykvvv)*
