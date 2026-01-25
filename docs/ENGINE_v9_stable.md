# Engine v9_stable - 技术文档

**版本：** v9_stable
**发布日期：** 2025-01-25

## 📋 概述

v9_stable 是 Bridge MCP 的稳定版本，在保持原有 3 工具接口不变的基础上，修复了所有已知 Bug 并新增了后台心跳和智能清理功能。

## 🎯 核心改进

### 1. 后台心跳线程

**问题：** v4 之前每次调用工具时才更新心跳，导致 ID 不稳定

**解决方案：**
```python
def _maintenance_loop():
    while True:
        _update_heartbeat(name, pid)  # 每 10 秒更新
        _clean_dead_local_peers()      # 每 10 秒清理
        _clean_remote_and_prune()      # 每 60 秒清理远程
        time.sleep(HEARTBEAT_INTERVAL)
```

**效果：**
- ID 保持稳定，不会每次调用都变化
- Agent 自动注册，无需手动管理

### 2. 智能 PID 清理

**问题：** Windows 上 `os.kill(pid, 0)` 不可靠，无法准确检测进程存活

**解决方案：** 使用 Windows API
```python
if os.name == "nt":
    import ctypes
    from ctypes import wintypes

    STILL_ACTIVE = 259
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

    h = OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
    if not h:
        err = ctypes.get_last_error()
        if err == 5:  # Access denied
            return True  # 当作存活
        return False

    code = wintypes.DWORD()
    ok = GetExitCodeProcess(h, ctypes.byref(code))
    CloseHandle(h)

    return code.value == STILL_ACTIVE
```

**效果：**
- 准确检测进程存活
- 避免误删活跃进程

### 3. 跨目录清理

**问题：** 只清理同目录的进程，其他目录的僵尸进程无法清理

**解决方案：**
```python
def _clean_dead_local_peers():
    my_host = SESSION_HOST

    rows = conn.execute(
        "SELECT id, pid FROM peers WHERE hostname=?",  # 只检查主机，不限制目录
        (my_host,),
    ).fetchall()

    for r in rows:
        if not _is_pid_alive(int(r["pid"])):
            delete(r["id"])
```

**效果：**
- 同机器所有目录的 Agent 都能被清理
- 不会误删其他机器的 Agent

### 4. 优雅退出

**问题：** 进程异常退出时注册信息残留

**解决方案：**
```python
def _remove_self():
    conn.execute("DELETE FROM peers WHERE id=?", (SESSION_NAME,))

atexit.register(_remove_self)  # 进程退出时清理
signal.signal(signal.SIGTERM, ...)  # 信号处理
signal.signal(signal.SIGINT, ...)
```

**效果：**
- 进程正常退出时自动清理
- 意外崩溃时由 TTL 机制清理

## 🗄️ 数据库 Schema

### Peers 表

```sql
CREATE TABLE peers (
    id TEXT PRIMARY KEY,        -- Agent ID (3 位数字)
    pid INTEGER,                 -- 进程 PID
    hostname TEXT,               -- 主机名（跨机器识别）
    last_seen REAL,              -- 最后心跳时间戳
    cwd TEXT                     -- 工作目录
);
```

**字段说明：**
- `id`: 3 位数字 ID（001-999）
- `hostname`: 机器名，区分不同机器
- `last_seen`: Unix 时间戳，用于 TTL 清理
- `cwd`: 工作目录，用于识别本地进程

### Messages 表

```sql
CREATE TABLE messages (
    msg_id TEXT PRIMARY KEY,     -- 消息 ID (UUID 前 8 位)
    ts REAL,                     -- Unix 时间戳
    ts_str TEXT,                 -- 可读时间字符串
    from_user TEXT,              -- 发送者 ID
    to_user TEXT,                -- 接收者 ID 或 "all"
    content TEXT,                -- 消息内容
    state TEXT DEFAULT 'queued', -- 状态：queued/inflight
    lease_owner TEXT,           -- Lease 持有者
    lease_until REAL,           -- Lease 过期时间
    attempt INTEGER DEFAULT 0,   -- 投递尝试次数
    delivered_at REAL            -- 实际投递时间
);
```

**状态转换：**
```
queued → inflight → deleted
   ↑            ↓
   └──── expired (lease 超时)
```

**关键字段：**
- `state`: 消息状态
- `lease_owner`: 持有 Lease 的 Agent ID
- `lease_until`: Lease 过期时间（now + 30秒）
- `attempt`: 投递次数（用于重试）

## ⚙️ 配置常量

```python
BRIDGE_DB_VERSION = "v9_stable"
BRIDGE_DB_FILENAME = f"bridge_{BRIDGE_DB_VERSION}.db"

HEARTBEAT_TTL = 300          # 5 分钟掉线（远程清理）
MSG_TTL = 86400              # 24 小时消息保留
LEASE_TTL = 30               # Lease 过期时间（秒）
MAX_BATCH_CHARS = 5000       # 单批最大字符数

HEARTBEAT_INTERVAL = 10.0    # 后台心跳间隔（秒）
CLEAN_LOCAL_EVERY = 10.0     # 本地清理间隔（秒）
CLEAN_REMOTE_EVERY = 60.0    # 远程清理间隔（秒）
CHECKPOINT_EVERY = 300.0     # 数据库优化间隔（秒）

RECV_TICK = 0.5               # recv 循环 sleep
RECV_DB_POLL_EVERY = 2.0     # recv 消息轮询间隔
```

## 🔄 消息传递流程

### 发送流程

1. 解析接收者列表（支持逗号分隔、"all"）
2. 查询在线 Peers
3. 对每个接收者写入一条消息（state='queued'）
4. 返回消息 ID

### 接收流程

1. **Lease 消息：**
   - 恢复过期的 inflight 消息
   - 读取 queued 消息
   - 标记为 inflight（设置 lease_owner, lease_until）

2. **处理消息：**
   - 格式化输出
   - ACK 删除消息（删除 state='inflight' 且 lease_owner=自己 的消息）

3. **异常处理：**
   - CancelledError/KeyboardInterrupt：释放 Lease（恢复为 queued）
   - 其他异常：释放 Lease

## 🧹 后台维护

### 本地清理（每 10 秒）

```python
def _clean_dead_local_peers():
    # 检查同机器的所有 Agent（不分目录）
    for peer in peers_on_same_host:
        if not _is_pid_alive(peer.pid):
            delete(peer.id)
```

**范围：** 同机器（hostname 相同），所有目录

### 远程清理（每 60 秒）

```python
def _clean_remote_and_prune():
    # 1. TTL 清理
    DELETE FROM peers WHERE last_seen < (now - HEARTBEAT_TTL)

    # 2. 恢复过期 Lease
    UPDATE messages SET state='queued', lease_owner=NULL
    WHERE state='inflight' AND lease_until < now

    # 3. 清理旧消息
    DELETE FROM messages WHERE ts < (now - MSG_TTL)
```

### 数据库优化（每 300 秒）

```python
PRAGMA wal_checkpoint(TRUNCATE)
PRAGMA optimize
```

## 🔒 安全与可靠性

### 进程检测

**Windows:**
- 使用 `OpenProcess` + `GetExitCodeProcess`
- 访问拒绝（err=5）当作存活（避免误删）

**POSIX:**
- `os.kill(pid, 0)` 配合 errno 处理
- `ESRCH` = 死, `EPERM` = 活

### 消息可靠性

**Lease 机制：**
- 消息被标记为 `inflight` 后，即使 Agent 崩溃也不会丢失
- Lease 过期（30 秒）后消息恢复为 `queued`
- 下次 `recv()` 时可以重新读取

**防重复：**
- 消息接收后立即删除（consume-on-read）
- 没有复杂的消息去重逻辑

## 📈 性能考虑

### 延迟

| 操作 | 延迟 |
|------|------|
| 取消响应 | ≤ 0.5 秒（RECV_TICK） |
| 消息轮询 | ≤ 2.5 秒（RECV_DB_POLL_EVERY + RECV_TICK） |
| 本地清理 | 每 10 秒 |
| 远程清理 | 每 60 秒 |
| 数据库优化 | 每 300 秒 |

### 扩展性

**限制：**
- SQLite 锁竞争（通过 WAL + busy_timeout 缓解）
- 网络模型（无真正的分布式锁）

**建议：**
- 小于 50 个 Agent
- 消息频率 < 10 条/秒
- 单条消息 < 10KB

## 🐛 已知限制

### ESC 打断导致连接关闭

**现象：** `recv()` 长时间监听时按 ESC 打断，可能触发 `AbortError`

**原因：** FastMCP + asyncio 的框架级限制

**解决方案：**
- 重启 MCP
- 或使用短周期：`recv(60)` 而不是 `recv(86400)`

---

**相关文档：**
- 产品主页：[README.md](../README.md)
- 中文主页：[README.zh-CN.md](../README.zh-CN.md)
- 使用手册：[PLAYBOOK.md](../PLAYBOOK.md) / [PLAYBOOK.zh-CN.md](../PLAYBOOK.zh-CN.md)
