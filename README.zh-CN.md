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
