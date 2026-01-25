# Bridge MCP v9_stable

**多 Agent 协作通信系统 - 稳定版**

## 📋 简介

Bridge MCP 是一个基于 SQLite 的轻量级多 Agent 协作通信系统，允许 AI Agents 之间进行可靠的点对点和公共频道消息传递。

**核心特性：**
- ✅ **后台心跳**：自动在线注册，无需手动管理连接
- ✅ **智能清理**：自动检测并清理死掉的 Agent 进程（支持跨目录）
- ✅ **消息可靠性**：Lease 机制确保消息不丢失
- ✅ **原子 ID 注册**：避免 ID 冲突
- ✅ **跨平台**：Windows/Linux/macOS 全平台支持
- ✅ **Windows 可靠 PID 检测**：使用 Windows API 准确检测进程存活

## 🚀 快速开始

### 安装依赖

```bash
pip install fastmcp
```

### 配置 MCP

在你的 Claude Desktop 配置文件中添加：

**Windows:**
```json
{
  "mcpServers": {
    "bridge": {
      "command": "python",
      "args": ["C:/ccbridge/bridge.py"]
    }
  }
}
```

**Linux/macOS:**
```json
{
  "mcpServers": {
    "bridge": {
      "command": "python",
      "args": ["/path/to/bridge.py"]
    }
  }
}
```

### 使用工具

```python
# 查看在线状态
get_status()

# 发送私信
send("123", "你好")           # 发送给 ID 123
send("123,456", "群发")       # 发送给多人
send("all", "公共广播")       # 公共频道

# 接收消息
recv()                         # 默认监听 1 小时
recv(60)                       # 监听 60 秒
```

## 📦 版本历史

### v9_stable (当前版本) - 2025-01-25

**修复的问题：**

| Bug | 描述 | 状态 |
|-----|------|------|
| Bug #1 | 数据丢失 | ✅ Lease 机制 |
| Bug #2 | 锁竞争 | ✅ 随机清理 + WAL |
| Bug #3 | ID 碰撞 | ✅ 原子 ID 注册 |
| Bug #4 | ID 爆炸 | ✅ 后台心跳线程 |
| Bug #5 | UPDATE 无 INSERT | ✅ UPDATE+INSERT 补偿 |

**新增功能：**

1. **后台心跳线程**
   - 每 10 秒自动更新心跳
   - ID 保持稳定，不会每次调用都变化

2. **智能进程清理**
   - 本地清理：每 10 秒检查同机器所有 Agent
   - 远程清理：TTL 机制（5 分钟无心跳自动下线）
   - 使用 Windows API (`OpenProcess` + `GetExitCodeProcess`) 可靠检测进程存活
   - 支持跨目录清理（同机器所有目录）

3. **优雅退出机制**
   - 进程退出时自动清理注册信息（`atexit`）
   - SIGTERM/SIGINT 信号处理

4. **跨目录清理支持**
   - 清理同机器的所有 Agent，不分目录
   - 只要 PID 死了就自动清理

**数据库版本：** `bridge_v9_stable.db`

---

### v4_pure (历史版本)

原始经典版本，功能完整但缺少后台心跳和智能清理。

## 🔧 工具说明

### get_status()
查看当前在线的 Agents。

**返回：** 你的 ID、主机名和在线列表

### send(to, content)
发送消息给其他 Agents。

**参数：**
- `to`: 接收者
  - `"123"` - 单个 ID
  - `"123,456"` - 多个 ID（逗号分隔）
  - `"all"` - 公共频道（所有在线 Agent，不包括自己）
- `content`: 消息内容

**返回：** 发送结果和消息 ID

### recv(wait_seconds)
接收消息。

**参数：**
- `wait_seconds`: 等待时间（秒），默认 3600（1 小时）

**返回：** 收到的消息或超时提示

**机制：**
- Lease 机制：消息被标记为 `inflight`
- ACK 后删除：读取成功后自动删除
- 未读保护：Agent 崩溃后消息恢复为 `queued`

**已知限制：**
- 如果客户端强行中止工具调用导致 MCP 连接关闭，需要重启 MCP

## 🛠️ 技术架构

### 数据库结构

**Peers 表：**
```sql
CREATE TABLE peers (
    id TEXT PRIMARY KEY,        -- Agent ID (3 位数字)
    pid INTEGER,                 -- 进程 PID
    hostname TEXT,               -- 主机名
    last_seen REAL,              -- 最后心跳时间
    cwd TEXT                     -- 工作目录
);
```

**Messages 表：**
```sql
CREATE TABLE messages (
    msg_id TEXT PRIMARY KEY,     -- 消息 ID
    ts REAL,                     -- 时间戳
    ts_str TEXT,                 -- 时间字符串
    from_user TEXT,              -- 发送者
    to_user TEXT,                -- 接收者
    content TEXT,                -- 消息内容
    state TEXT DEFAULT 'queued', -- 状态: queued/inflight
    lease_owner TEXT,           -- Lease 持有者
    lease_until REAL,           -- Lease 过期时间
    attempt INTEGER,             -- 尝试次数
    delivered_at REAL            -- 投递时间
);
```

### 后台维护

**本地清理（每 10 秒）：**
```python
_clean_dead_local_peers():
    # 检查同机器的所有 Agent（不分目录）
    for peer in peers_on_same_host:
        if not _is_pid_alive(peer.pid):
            delete(peer)
```

**Windows PID 检测（使用 ctypes）：**
```python
OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
GetExitCodeProcess(handle, exit_code)
return exit_code == STILL_ACTIVE  # 259
```

**远程清理（每 60 秒）：**
- 清理超过 TTL（300 秒）的过期 Peers
- 恢复过期的 Lease
- 清理旧消息（24 小时）

**数据库优化（每 300 秒）：**
- WAL checkpoint
- 数据库优化

### 消息传递机制

1. **发送**：消息写入数据库，状态为 `queued`
2. **接收**：
   - 消息被标记为 `inflight`（Lease，30 秒 TTL）
   - Agent 处理后确认（ACK），删除消息
   - 如果 Agent 崩溃，Lease 过期后消息恢复为 `queued`

## 🎯 使用场景

### 1. 基础协作

```python
# Agent A (管理者)
get_status()  # 查看在线: 123, 456, 789
send("123,456", "开始任务X")  # 分配任务

# Agent B (工作者)
recv(3600)  # 监听 1 小时
# 收到消息后处理任务
# 完成后发送报告
send("789", "任务X已完成")
```

### 2. 公共频道协调

```python
# 管理者
send("all", "所有人报告状态")
recv(60)  # 收集报告

# 工作者（持续监听）
recv(3600)
# 收到公共频道消息后回复，然后继续监听
```

### 3. 多播通信

```python
# 同时给多人发消息
send("123,456,789", "紧急通知")

# 接收多个人的回复
recv(120)
```

## 📝 已知限制

### ESC 打断导致连接关闭

**现象：** 在 `recv()` 长时间监听时按 ESC 打断，可能触发 `AbortError`，导致 MCP 连接关闭。

**原因：** FastMCP + asyncio 的框架级限制

**解决方案：**
- 重启 MCP 恢复连接
- 使用短周期监听：`recv(60)` 而不是 `recv(86400)`

## 🔒 安全性

- **本地清理**：只检查同机器的进程
- **进程检测**：使用 Windows API 可靠检测 PID 存活
- **访问拒绝处理**：无法查询的进程当作存活（避免误删）
- **保守策略**：任何无法确定的进程都当作存活

## 📂 文件说明

```
bridge.py                 # 主程序（包含所有功能）
bridge_v9_stable.db      # 数据库文件（自动创建）
```

## 🛠️ 开发者

### 修改配置

编辑 `bridge.py` 中的常量：

```python
HEARTBEAT_TTL = 300          # 5 分钟掉线
MSG_TTL = 86400              # 24 小时消息保留
LEASE_TTL = 30               # Lease 过期时间
RECV_TICK = 0.5              # 取消响应速度
HEARTBEAT_INTERVAL = 10.0    # 心跳间隔
```

### 数据库路径

- **Windows 优先**: `C:/mcp_msg_pool/`
- **Windows 备用**: `C:/Users/Public/mcp_msg_pool/`
- **Linux/macOS**: `~/.mcp_msg_pool/` 或 `/tmp/mcp_msg_pool/`

## 🙏 致谢

基于原始的 ai-workgroup-mcp 项目进行改进和优化。

**原作者：** vvvykvvv

**v9_stable 改进：**
- 修复 Bug #1-#5
- 添加后台心跳线程
- 实现 Windows 可靠 PID 检测
- 支持跨目录清理
- 优雅退出机制

## 📄 许可证

与原项目保持一致。

---

**更多示例：** [PLAYBOOK.md](PLAYBOOK.md) / [PLAYBOOK.zh-CN.md](PLAYBOOK.zh-CN.md)

*Powered by Bridge MCP (by vvvykvvv)*
