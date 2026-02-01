# CCBridge v12 - Isolated Database Architecture Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 重构 CCBridge MCP 服务器，从共享双数据库架构迁移到每个 Agent 独立数据库架构，彻底消除并发锁竞争，提高稳定性。

**Architecture:** 每个 Agent 拥有独立的 SQLite 数据库文件（bridge_agent_XXX.db），只读写自己的数据。Leader 通过串行扫描各个数据库，将消息从发送者的 outbox 搬运到接收者的 inbox。无共享状态，无锁竞争。

**Tech Stack:** Python 3.10+, SQLite (WAL mode), FastMCP, asyncio, threading

---

## Task 1: 创建数据库模块 (db.py)

**Files:**
- Create: `C:\ccbridge\bridge_v12\db.py`

**Step 1: 创建数据库管理模块骨架**

```python
# C:\ccbridge\bridge_v12\db.py
import os
import time
import sqlite3
from pathlib import Path
from contextlib import contextmanager
from typing import Optional

# ============================================================
# CCBridge v12 - Isolated Database Architecture
# 每个 Agent 拥有独立的数据库文件
# ============================================================

# --- 配置 ---
DB_ROOT = Path("C:/mcp_msg_pool")
HEARTBEAT_TTL = 60  # 心跳过期时间（秒）
DB_TIMEOUT = 2.0    # 数据库连接超时

def ensure_db_root():
    """确保数据库根目录存在"""
    DB_ROOT.mkdir(parents=True, exist_ok=True)

@contextmanager
def open_db(agent_id: str, timeout: float = DB_TIMEOUT):
    """
    打开指定 Agent 的数据库

    Args:
        agent_id: Agent ID（如 "788"）
        timeout: 连接超时时间
    """
    db_path = DB_ROOT / f"bridge_agent_{agent_id}.db"
    conn = None
    try:
        conn = sqlite3.connect(str(db_path), timeout=timeout)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        conn.execute("PRAGMA busy_timeout=5000;")
        yield conn
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass

def init_db(agent_id: str, pid: int, hostname: str, cwd: str):
    """
    初始化 Agent 的数据库，创建所有表

    Args:
        agent_id: Agent ID
        pid: 进程 PID
        hostname: 主机名
        cwd: 工作目录
    """
    db_path = DB_ROOT / f"bridge_agent_{agent_id}.db"

    # 如果文件已存在且可读，直接返回（复用场景）
    if db_path.exists():
        try:
            with open_db(agent_id) as conn:
                conn.execute("SELECT 1 FROM self_state WHERE key='main'")
            return
        except Exception:
            # 文件损坏，删除重建
            db_path.unlink(missing_ok=True)

    # 创建新数据库
    with open_db(agent_id) as conn:
        # self_state 表
        conn.execute("""
            CREATE TABLE self_state (
                key TEXT PRIMARY KEY,
                last_heartbeat REAL,
                pid INTEGER,
                hostname TEXT,
                cwd TEXT,
                mode TEXT,
                mode_since REAL,
                recv_started REAL,
                recv_deadline REAL,
                recv_wait_seconds INTEGER,
                status_request INTEGER DEFAULT 0,
                active_last_touch REAL
            )
        """)

        # inbox 表
        conn.execute("""
            CREATE TABLE inbox (
                msg_id TEXT PRIMARY KEY,
                ts REAL,
                ts_str TEXT,
                from_id TEXT,
                content TEXT
            )
        """)
        conn.execute("CREATE INDEX idx_inbox_ts ON inbox(ts)")

        # outbox 表
        conn.execute("""
            CREATE TABLE outbox (
                msg_id TEXT PRIMARY KEY,
                ts REAL,
                ts_str TEXT,
                to_id TEXT,
                content TEXT,
                send_deadline REAL,
                state TEXT DEFAULT 'pending'
            )
        """)
        conn.execute("CREATE INDEX idx_outbox_ts ON outbox(ts)")

        # status_result 表
        conn.execute("""
            CREATE TABLE status_result (
                key TEXT PRIMARY KEY,
                result TEXT,
                updated_at REAL
            )
        """)

        # 初始化 self_state
        now = time.time()
        conn.execute("""
            INSERT INTO self_state (
                key, last_heartbeat, pid, hostname, cwd,
                mode, mode_since, active_last_touch
            ) VALUES ('main', ?, ?, ?, ?, 'working', ?, ?)
        """, (now, pid, hostname, cwd, now, now))

def claim_id() -> str:
    """
    获取一个可用的 Agent ID

    Returns:
        三位数 ID 字符串，如 "788"
    """
    ensure_db_root()

    # 主要路径：找第一个文件不存在的 ID
    for candidate_id in range(1, 1000):
        cid = f"{candidate_id:03d}"
        db_path = DB_ROOT / f"bridge_agent_{cid}.db"
        if not db_path.exists():
            return cid

    # 极罕见：999 个文件都存在，找最旧的过期 ID
    now = time.time()
    oldest_id = None
    oldest_heartbeat = float('inf')

    for candidate_id in range(1, 1000):
        cid = f"{candidate_id:03d}"
        db_path = DB_ROOT / f"bridge_agent_{cid}.db"
        if db_path.exists():
            try:
                with open_db(cid) as conn:
                    row = conn.execute(
                        "SELECT last_heartbeat FROM self_state WHERE key='main'"
                    ).fetchone()
                    if row:
                        hb = row["last_heartbeat"]
                        if now - hb > HEARTBEAT_TTL and hb < oldest_heartbeat:
                            oldest_id = cid
                            oldest_heartbeat = hb
            except Exception:
                # 文件损坏，可以直接复用
                return cid

    if oldest_id:
        # 删除旧文件
        db_path = DB_ROOT / f"bridge_agent_{oldest_id}.db"
        db_path.unlink(missing_ok=True)
        return oldest_id

    raise RuntimeError("ID pool exhausted (1-999 all in use)")

def scan_online_agents() -> list[str]:
    """
    扫描所有在线的 Agent

    Returns:
        在线 Agent ID 列表，按 ID 排序
    """
    ensure_db_root()
    online_agents = []
    now = time.time()

    for db_path in DB_ROOT.glob("bridge_agent_*.db"):
        agent_id = db_path.stem.replace("bridge_agent_", "")
        try:
            with open_db(agent_id, timeout=0.5) as conn:
                row = conn.execute(
                    "SELECT last_heartbeat FROM self_state WHERE key='main'"
                ).fetchone()
                if row and now - row["last_heartbeat"] < HEARTBEAT_TTL:
                    online_agents.append(agent_id)
        except Exception:
            continue

    return sorted(online_agents)
```

**Step 2: 提交**

```bash
cd C:\ccbridge
git add bridge_v12/db.py
git commit -m "feat(v12): create database module with isolated DB architecture"
```

---

## Task 2: 创建会话管理模块 (session.py)

**Files:**
- Create: `C:\ccbridge\bridge_v12\session.py`

**Step 1: 创建会话管理模块**

```python
# C:\ccbridge\bridge_v12\session.py
import os
import time
import platform
import threading
import atexit
from typing import Optional

from .db import init_db, claim_id, open_db

# --- 全局会话状态 ---
SESSION_ID: Optional[str] = None
SESSION_PID = os.getpid()
SESSION_HOST = platform.node()
LAST_ACTIVE_TS = 0.0

_background_started = False
_background_lock = threading.Lock()

def get_session() -> tuple[str, int]:
    """
    获取当前会话的 ID 和 PID

    Returns:
        (agent_id, pid) 元组
    """
    global SESSION_ID
    if not SESSION_ID:
        SESSION_ID = claim_id()
        cwd = os.getcwd()
        init_db(SESSION_ID, SESSION_PID, SESSION_HOST, cwd)
        _update_heartbeat()
    return SESSION_ID, SESSION_PID

def _update_heartbeat():
    """更新自己的心跳时间"""
    if not SESSION_ID:
        return
    now = time.time()
    cwd = os.getcwd()
    try:
        with open_db(SESSION_ID) as conn:
            conn.execute("""
                UPDATE self_state SET
                    last_heartbeat = ?,
                    cwd = ?,
                    active_last_touch = COALESCE(?, active_last_touch)
                WHERE key='main'
            """, (now, cwd, (LAST_ACTIVE_TS if LAST_ACTIVE_TS > 0 else None)))
    except Exception:
        pass

def mark_active():
    """标记当前会话为活跃状态"""
    global LAST_ACTIVE_TS
    LAST_ACTIVE_TS = time.time()

def _heartbeat_loop():
    """后台心跳循环"""
    while True:
        _update_heartbeat()
        time.sleep(10.0)

def _ensure_background_started():
    """确保后台线程已启动"""
    global _background_started
    if _background_started:
        return
    with _background_lock:
        if _background_started:
            return
        import threading
        t = threading.Thread(target=_heartbeat_loop, daemon=True)
        t.start()
        _background_started = True

def _cleanup_self():
    """清理自己的数据库（退出时）"""
    if not SESSION_ID:
        return
    # 注意：这里不删除数据库文件，保留用于历史记录
    # 只是把 heartbeat 标记为过期即可
    pass

atexit.register(_cleanup_self)

# 启动时初始化
get_session()
_ensure_background_started()
```

**Step 2: 提交**

```bash
git add bridge_v12/session.py
git commit -m "feat(v12): add session management with heartbeat"
```

---

## Task 3: 创建 Leader 维护模块 (leader.py)

**Files:**
- Create: `C:\ccbridge\bridge_v12\leader.py`

**Step 1: 创建 Leader 维护模块**

```python
# C:\ccbridge\bridge_v12\leader.py
import time
import glob
import uuid
from typing import Optional

from .db import open_db, scan_online_agents
from .session import get_session

# --- 配置 ---
BATCH_SIZE_LIMIT = 50  # 每次 Leader 扫描单个 Agent 的最大消息数

def get_leader_id(online_agents: list[str]) -> Optional[str]:
    """
    获取当前的 Leader ID（ID 最小的在线 Agent）

    Args:
        online_agents: 在线 Agent ID 列表

    Returns:
        Leader ID，如果没有在线 Agent 则返回 None
    """
    if not online_agents:
        return None
    return min(online_agents)

def is_i_leader(online_agents: list[str]) -> bool:
    """
    判断我是否是 Leader

    Args:
        online_agents: 在线 Agent ID 列表

    Returns:
        是否是 Leader
    """
    my_id, _ = get_session()
    leader_id = get_leader_id(online_agents)
    return leader_id == my_id

def deliver_message(msg: dict, online_agents: list[str]) -> bool:
    """
    将消息从 outbox 搬运到目标 inbox

    Args:
        msg: 消息记录（包含 msg_id, to_id, content, ts_str, from_id）
        online_agents: 在线 Agent ID 列表

    Returns:
        是否成功搬运
    """
    to_id = msg["to_id"]
    from_id = msg.get("from_id", "unknown")

    targets = []
    if to_id == "all":
        # 广播：所有在线 Agent（除了发送者）
        targets = [aid for aid in online_agents if aid != from_id]
    else:
        # 单播
        if to_id in online_agents:
            targets = [to_id]
        else:
            # 目标离线，返回 False
            return False

    # 写入所有目标的 inbox
    now = time.time()
    for target_id in targets:
        try:
            with open_db(target_id) as conn:
                conn.execute("""
                    INSERT INTO inbox (msg_id, ts, ts_str, from_id, content)
                    VALUES (?, ?, ?, ?, ?)
                """, (msg["msg_id"], msg["ts"], msg["ts_str"], from_id, msg["content"]))
        except Exception:
            return False

    return True

def process_one_agent(agent_id: str, online_agents: list[str]):
    """
    Leader 处理单个 Agent 的所有待办事项

    Args:
        agent_id: 要处理的 Agent ID
        online_agents: 当前在线 Agent 列表
    """
    db_path = f"C:/mcp_msg_pool/bridge_agent_{agent_id}.db"

    # 快照读取
    outbox_msgs = []
    has_request = False

    try:
        with open_db(agent_id) as conn:
            # 1. 读取 outbox（最多 50 条）
            rows = conn.execute(
                "SELECT * FROM outbox ORDER BY ts LIMIT ?",
                (BATCH_SIZE_LIMIT,)
            ).fetchall()
            outbox_msgs = [dict(r) for r in rows]

            # 2. 读取 status_request
            row = conn.execute(
                "SELECT status_request FROM self_state WHERE key='main'"
            ).fetchone()
            has_request = row and row["status_request"] == 1
    except Exception:
        return

    # 在 DB 外处理消息
    delivered_ids = []
    for msg in outbox_msgs:
        if deliver_message(msg, online_agents):
            delivered_ids.append(msg["msg_id"])

    # 重新打开，清理 + 写结果
    try:
        with open_db(agent_id) as conn:
            # 删除已搬运的
            if delivered_ids:
                placeholders = ",".join("?" * len(delivered_ids))
                conn.execute(f"DELETE FROM outbox WHERE msg_id IN ({placeholders})", delivered_ids)

            # 写 status_result
            if has_request:
                all_status = format_all_agent_status(online_agents)
                conn.execute("""
                    INSERT OR REPLACE INTO status_result (key, result, updated_at)
                    VALUES ('main', ?, ?)
                """, (all_status, time.time()))
                conn.execute("UPDATE self_state SET status_request=0 WHERE key='main'")
    except Exception:
        pass

def format_all_agent_status(online_agents: list[str]) -> str:
    """
    格式化所有 Agent 的状态字符串

    Args:
        online_agents: 在线 Agent ID 列表

    Returns:
        格式化的状态字符串
    """
    my_id, _ = get_session()
    now = time.time()

    agents_info = []

    for agent_id in sorted(online_agents):
        try:
            with open_db(agent_id) as conn:
                row = conn.execute(
                    "SELECT * FROM self_state WHERE key='main'"
                ).fetchone()

                if not row:
                    continue

                info = dict(row)
                flags = []
                if agent_id == my_id:
                    flags.append("THIS")

                # 计算状态
                state_str = ""
                if info.get("mode") == "waiting" and info.get("recv_started"):
                    elapsed = max(0, int(now - float(info["recv_started"])))
                    total = info.get("recv_wait_seconds") or 0
                    state_str = f"🎧 Waiting ({elapsed}s/{int(total)}s)" if total else f"🎧 Waiting ({elapsed}s)"
                else:
                    since = info.get("mode_since") or info.get("active_last_touch")
                    if since:
                        w_elapsed = max(0, int(now - float(since)))
                        state_str = f"❓ Working ({w_elapsed}s)" if w_elapsed >= 1800 else f"🛠 Working ({w_elapsed}s)"
                    else:
                        state_str = "🛠 Working (0s)"

                bracket = " | ".join([*flags, state_str])
                cwd = info.get("cwd") or info.get("hostname") or "UnknownPath"
                line = f"Agent {agent_id} @ {cwd}  [{bracket}]"
                agents_info.append((agent_id == my_id, agent_id, line))
        except Exception:
            continue

    # 排序：自己在前，其他按 ID
    agents_info.sort(key=lambda x: (0, x[1]) if x[0] else (1, x[1]))

    lines = [line for _, _, line in agents_info]
    return "\n".join(lines) if lines else "No active agents."

def leader_maintenance_cycle():
    """
    Leader 维护循环的一个周期

    Returns:
        (is_leader, online_count) 元组
    """
    # 扫描在线 Agent
    online_agents = scan_online_agents()

    if not online_agents:
        return False, 0

    # 判断是否是 Leader
    if not is_i_leader(online_agents):
        return False, len(online_agents)

    # 我是 Leader，处理每个 Agent
    for agent_id in sorted(online_agents):
        process_one_agent(agent_id, online_agents)

    return True, len(online_agents)
```

**Step 2: 提交**

```bash
git add bridge_v12/leader.py
git commit -m "feat(v12): add leader maintenance module"
```

---

## Task 4: 创建后台维护循环 (maintenance.py)

**Files:**
- Create: `C:\ccbridge\bridge_v12\maintenance.py`

**Step 1: 创建后台维护循环**

```python
# C:\ccbridge\bridge_v12\maintenance.py
import time
import threading
import random

from .leader import leader_maintenance_cycle
from .session import get_session

# --- 配置 ---
BASE_POLL_INTERVAL = 0.5  # 基础轮询间隔（秒）

def _maintenance_loop():
    """后台维护循环"""
    while True:
        cycle_start = time.time()

        try:
            is_leader, agent_count = leader_maintenance_cycle()

            # 动态调整轮询间隔
            if agent_count > 0:
                poll_interval = max(0.1, BASE_POLL_INTERVAL / agent_count)
            else:
                poll_interval = 1.0

            elapsed = time.time() - cycle_start
            sleep_time = max(0, poll_interval - elapsed)

            # 添加微小随机抖动，避免多个 Agent 同步
            sleep_time += random.random() * 0.05

            time.sleep(sleep_time)
        except Exception:
            # 维护循环出错，短暂休眠后继续
            time.sleep(1.0)

_maintenance_started = False
_maintenance_lock = threading.Lock()

def ensure_maintenance_started():
    """确保后台维护循环已启动"""
    global _maintenance_started
    if _maintenance_started:
        return
    with _maintenance_lock:
        if _maintenance_started:
            return
        t = threading.Thread(target=_maintenance_loop, daemon=True)
        t.start()
        _maintenance_started = True
```

**Step 2: 提交**

```bash
git add bridge_v12/maintenance.py
git commit -m "feat(v12): add background maintenance loop"
```

---

## Task 5: 创建消息处理模块 (messaging.py)

**Files:**
- Create: `C:\ccbridge\bridge_v12\messaging.py`

**Step 1: 创建消息处理模块**

```python
# C:\ccbridge\bridge_v12\messaging.py
import time
import uuid

from .db import open_db
from .session import get_session, mark_active

def send(to: str, content: str) -> str:
    """
    发送消息给指定 Agent 或所有 Agent

    Args:
        to: 目标 ID，或 "all" 表示广播，或逗号分隔的多个 ID
        content: 消息内容

    Returns:
        发送结果字符串
    """
    mark_active()
    my_id, _ = get_session()

    # 解析收件人列表
    recipients = [r.strip() for r in to.split(",") if r.strip()]

    # 检查是否发送给自己
    if my_id in recipients:
        return "Error: cannot send to self."

    # 处理 "all"
    if any(r.lower() == "all" for r in recipients):
        from .db import scan_online_agents
        online = scan_online_agents()
        final = [aid for aid in online if aid != my_id]
        if not final:
            return "No other agents online."
        recipients = final
    else:
        # 验证收件人在线
        from .db import scan_online_agents
        online = scan_online_agents()
        for r in recipients:
            if r not in online:
                return f"Error: Agent '{r}' offline."

    # 写入自己的 outbox
    ts = time.time()
    ts_str = time.strftime("%H:%M:%S")

    msg_ids = []
    first_short = None
    for to_id in recipients:
        msg_id = uuid.uuid4().hex
        if not first_short:
            first_short = msg_id[:8]
        msg_ids.append(msg_id)

        try:
            with open_db(my_id) as conn:
                conn.execute("""
                    INSERT INTO outbox (msg_id, ts, ts_str, to_id, content, send_deadline)
                    VALUES (?, ?, ?, ?, ?, ?)
                """, (msg_id, ts, ts_str, to_id, content, ts + 2.0))
        except Exception as e:
            return f"DB Error: {e}"

    # 等待 Leader 搬运（最多 2 秒）
    deadline = time.time() + 2.0
    check_interval = 0.1

    while time.time() < deadline:
        remaining = []
        delivered = []
        for msg_id in msg_ids:
            try:
                with open_db(my_id) as conn:
                    row = conn.execute(
                        "SELECT state FROM outbox WHERE msg_id=?",
                        (msg_id,)
                    ).fetchone()
                    if not row:
                        # 已被删除 = 已搬运
                        delivered.append(msg_id)
                    else:
                        remaining.append(msg_id)
            except Exception:
                remaining.append(msg_id)

        msg_ids = remaining
        if not msg_ids:
            # 全部搬运完成
            return f"Sent (to {len(delivered)} agent(s), id={first_short})"

        time.sleep(check_interval)

    # 超时，检查是否部分成功
    if delivered:
        return f"Partially sent (to {len(delivered)}/{len(recipients)} agents, id={first_short})"

    return f"Send timeout after 2s (to {len(recipients)} agents)"

def recv(wait_seconds: int = 86400) -> str:
    """
    接收消息

    Args:
        wait_seconds: 等待超时时间（秒），默认 24 小时

    Returns:
        接收到的消息字符串，或超时/取消消息
    """
    mark_active()
    my_id, _ = get_session()
    start_time = time.time()
    my_task_ts = get_last_active_timestamp()

    # 立即检查一次
    messages = fetch_inbox_messages()
    if messages:
        return format_messages(messages)

    if wait_seconds <= 0:
        return "No new messages."

    # 标记为等待模式
    set_waiting_mode(wait_seconds)
    waiting_marked = True

    try:
        while True:
            # 检查是否被新命令打断
            current_ts = get_last_active_timestamp()
            if current_ts != my_task_ts:
                return "Cancelled by new command."

            # 检查超时
            elapsed = time.time() - start_time
            if elapsed >= float(wait_seconds):
                return f"Timeout ({int(wait_seconds)}s)."

            # 检查 inbox
            messages = fetch_inbox_messages()
            if messages:
                return format_messages(messages)

            # 轮询间隔
            time.sleep(0.25)

    finally:
        if waiting_marked:
            clear_waiting_mode()

def fetch_inbox_messages() -> list[dict]:
    """获取 inbox 中的所有消息并清空"""
    my_id, _ = get_session()
    try:
        with open_db(my_id) as conn:
            rows = conn.execute(
                "SELECT * FROM inbox ORDER BY ts"
            ).fetchall()
            messages = [dict(r) for r in rows]

            # 清空 inbox
            conn.execute("DELETE FROM inbox")

            return messages
    except Exception:
        return []

def format_messages(messages: list[dict]) -> str:
    """格式化消息列表"""
    if not messages:
        return "No messages."

    from collections import defaultdict
    grouped = defaultdict(list)
    for m in messages:
        grouped[m["from_id"]].append(m)

    senders = sorted(grouped.keys(), key=lambda s: min(mm["ts"] for mm in grouped[s]))

    lines = [f"=== {len(messages)} messages from {len(grouped)} agent(s) ===\n"]

    for sender in senders:
        msgs = grouped[sender]
        lines.append(f"[{sender}] - {len(msgs)} message(s)")
        for m in msgs:
            lines.append(f"  {m['ts_str']} {m['content']}")
        lines.append("")

    return "\n".join(lines)

def get_last_active_timestamp() -> float:
    """获取最后活跃时间戳"""
    from .session import LAST_ACTIVE_TS
    return LAST_ACTIVE_TS

def set_waiting_mode(wait_seconds: int):
    """设置为等待模式"""
    my_id, _ = get_session()
    now = time.time()
    try:
        with open_db(my_id) as conn:
            conn.execute("""
                UPDATE self_state SET
                    mode='waiting',
                    mode_since=?,
                    recv_started=?,
                    recv_deadline=?,
                    recv_wait_seconds=?
                WHERE key='main'
            """, (now, now, now + float(wait_seconds), wait_seconds))
    except Exception:
        pass

def clear_waiting_mode():
    """清除等待模式"""
    my_id, _ = get_session()
    now = time.time()
    try:
        with open_db(my_id) as conn:
            conn.execute("""
                UPDATE self_state SET
                    mode='working',
                    mode_since=?,
                    recv_started=NULL,
                    recv_deadline=NULL,
                    recv_wait_seconds=NULL
                WHERE key='main'
            """, (now,))
    except Exception:
        pass
```

**Step 2: 提交**

```bash
git add bridge_v12/messaging.py
git commit -m "feat(v12): add messaging module with send/recv"
```

---

## Task 6: 创建 MCP 工具 (tools.py)

**Files:**
- Create: `C:\ccbridge\bridge_v12\tools.py`

**Step 1: 创建 MCP 工具封装**

```python
# C:\ccbridge\bridge_v12\tools.py
import time
import asyncio

from mcp.server.fastmcp import FastMCP
from .session import get_session, mark_active, ensure_maintenance_started
from .messaging import send, recv
from .db import open_db

mcp = FastMCP("RootBridge-v12")

# 初始化
get_session()
ensure_maintenance_started()

@mcp.tool()
def get_status() -> str:
    """List online agents."""
    mark_active()
    my_id, _ = get_session()

    # 在自己的 DB 标记请求
    try:
        with open_db(my_id) as conn:
            conn.execute("UPDATE self_state SET status_request=1 WHERE key='main'")
    except Exception:
        pass

    # 等待 Leader 响应（最多 3 秒）
    deadline = time.time() + 3.0
    while time.time() < deadline:
        try:
            with open_db(my_id) as conn:
                row = conn.execute(
                    "SELECT result, updated_at FROM status_result WHERE key='main'"
                ).fetchone()
                if row:
                    updated = row["updated_at"] or 0
                    # 检查是否是最近的结果（3秒内）
                    if time.time() - updated < 3.0:
                        return row["result"]
        except Exception:
            pass
        time.sleep(0.1)

    # 降级：自己扫描返回
    from .leader import format_all_agent_status
    from .db import scan_online_agents
    online = scan_online_agents()
    return format_all_agent_status(online)

@mcp.tool()
def send(to: str, content: str) -> str:
    """Send message to 'id' or 'all'."""
    from .messaging import send as _send
    return _send(to, content)

@mcp.tool()
async def recv(wait_seconds: int = 86400) -> str:
    """Receive messages."""
    from .messaging import recv as _recv
    # 在线程池中执行，避免阻塞事件循环
    return await asyncio.to_thread(_recv, wait_seconds)

if __name__ == "__main__":
    mcp.run()
```

**Step 2: 提交**

```bash
git add bridge_v12/tools.py
git commit -m "feat(v12): add MCP tools (get_status, send, recv)"
```

---

## Task 7: 创建包初始化文件

**Files:**
- Create: `C:\ccbridge\bridge_v12\__init__.py`

**Step 1: 创建空的 __init__.py**

```python
# C:\ccbridge\bridge_v12\__init__.py
"""CCBridge v12 - Isolated Database Architecture"""

__version__ = "v12"
```

**Step 2: 提交**

```bash
git add bridge_v12/__init__.py
git commit -m "feat(v12): add package init file"
```

---

## Task 8: 创建入口脚本

**Files:**
- Create: `C:\ccbridge\bridge_v12_main.py`

**Step 1: 创建独立运行入口**

```python
#!/usr/bin/env python3
# C:\ccbridge\bridge_v12_main.py
"""
CCBridge v12 主入口

运行方式：
    python bridge_v12_main.py

或作为 MCP 服务器：
    mcp dev bridge_v12_main.py
"""

import sys
import os

# 添加当前目录到 Python 路径
sys.path.insert(0, os.path.dirname(__file__))

from bridge_v12.tools import mcp

if __name__ == "__main__":
    mcp.run()
```

**Step 2: 提交**

```bash
git add bridge_v12_main.py
git commit -m "feat(v12): add main entry point script"
```

---

## Task 9: 测试基本功能

**Files:**
- Create: `C:\ccbridge\tests\test_v12_basic.py`

**Step 1: 创建基本测试**

```python
# C:\ccbridge\tests\test_v12_basic.py
import pytest
import time
import tempfile
import shutil
from pathlib import Path

# 修改 DB_ROOT 为临时目录
import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

from bridge_v12 import db
from bridge_v12.session import get_session, mark_active
from bridge_v12.messaging import send, recv

@pytest.fixture
def temp_db_root():
    """临时数据库根目录"""
    original_root = db.DB_ROOT
    temp_dir = tempfile.mkdtemp()
    db.DB_ROOT = Path(temp_dir)
    db.ensure_db_root()

    yield temp_dir

    # 清理
    shutil.rmtree(temp_dir, ignore_errors=True)
    db.DB_ROOT = original_root

def test_claim_id(temp_db_root):
    """测试 ID 获取"""
    id1 = db.claim_id()
    assert id1 == "001"  # 第一个 ID 应该是 001

    id2 = db.claim_id()
    assert id2 == "002"  # 第二个 ID 应该是 002

def test_init_db(temp_db_root):
    """测试数据库初始化"""
    agent_id = db.claim_id()
    db.init_db(agent_id, 12345, "test-host", "/test/path")

    # 检查文件是否存在
    db_path = db.DB_ROOT / f"bridge_agent_{agent_id}.db"
    assert db_path.exists()

    # 检查表是否存在
    with db.open_db(agent_id) as conn:
        tables = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
        table_names = [t["name"] for t in tables]
        assert "self_state" in table_names
        assert "inbox" in table_names
        assert "outbox" in table_names
        assert "status_result" in table_names

def test_scan_online_agents(temp_db_root):
    """测试扫描在线 Agent"""
    # 创建 3 个 Agent
    agents = []
    for i in range(3):
        agent_id = db.claim_id()
        db.init_db(agent_id, 1000 + i, f"host-{i}", f"/path-{i}")
        agents.append(agent_id)

    # 扫描
    online = db.scan_online_agents()

    # 应该找到所有 3 个
    assert len(online) == 3
    assert set(online) == set(agents)

def test_send_to_all(temp_db_root):
    """测试广播消息"""
    # 创建 2 个 Agent
    agent1 = db.claim_id()
    db.init_db(agent1, 1001, "host-1", "/path-1")

    agent2 = db.claim_id()
    db.init_db(agent2, 1002, "host-2", "/path-2")

    # Agent 1 发送广播
    # 注意：这里需要模拟 Leader 搬运
    # 简化测试：直接检查 outbox
    with db.open_db(agent1) as conn:
        conn.execute("""
            INSERT INTO outbox (msg_id, ts, ts_str, to_id, content, send_deadline)
            VALUES ('test123', ?, ?, ?, ?, ?)
        """, (time.time(), "12:00:00", "all", "hello", time.time() + 2))

    # 检查 outbox
    with db.open_db(agent1) as conn:
        rows = conn.execute("SELECT * FROM outbox").fetchall()
        assert len(rows) == 1
        assert rows[0]["to_id"] == "all"
```

**Step 2: 运行测试**

```bash
cd C:\ccbridge
pytest tests/test_v12_basic.py -v
```

**Step 3: 提交**

```bash
git add tests/test_v12_basic.py
git commit -m "test(v12): add basic functionality tests"
```

---

## Task 10: 端到端测试

**Files:**
- Create: `C:\ccbridge\tests\test_v12_e2e.py`

**Step 1: 创建端到端测试**

```python
# C:\ccbridge\tests\test_v12_e2e.py
import pytest
import time
import tempfile
import shutil
import asyncio
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parent.parent))

from bridge_v12 import db
from bridge_v12.session import get_session, _ensure_background_started
from bridge_v12.leader import leader_maintenance_cycle, process_one_agent
from bridge_v12.messaging import send, recv

@pytest.fixture
def two_agents(temp_db_root):
    """创建两个在线 Agent"""
    agents = []
    sessions = []

    for i in range(2):
        # 模拟新会话
        agent_id = db.claim_id()
        db.init_db(agent_id, 2000 + i, f"test-host-{i}", f"/test/path-{i}")
        agents.append(agent_id)

    yield agents

    # 清理
    for agent_id in agents:
        db_path = db.DB_ROOT / f"bridge_agent_{agent_id}.db"
        db_path.unlink(missing_ok=True)

def test_send_and_receive(two_agents):
    """测试发送和接收消息"""
    agent1, agent2 = two_agents

    # Agent 1 发送消息给 Agent 2
    ts = time.time()
    with db.open_db(agent1) as conn:
        conn.execute("""
            INSERT INTO outbox (msg_id, ts, ts_str, to_id, content, send_deadline)
            VALUES ('msg001', ?, ?, ?, ?, ?)
        """, (ts, "12:00:00", agent2, "hello from agent1", ts + 2))

    # 模拟 Leader 搬运
    online = db.scan_online_agents()
    process_one_agent(agent1, online)

    # 检查 Agent 2 的 inbox
    with db.open_db(agent2) as conn:
        rows = conn.execute("SELECT * FROM inbox").fetchall()
        assert len(rows) == 1
        assert rows[0]["from_id"] == agent1
        assert rows[0]["content"] == "hello from agent1"

    # 检查 Agent 1 的 outbox 已清空
    with db.open_db(agent1) as conn:
        rows = conn.execute("SELECT * FROM outbox").fetchall()
        assert len(rows) == 0

def test_broadcast(two_agents):
    """测试广播消息"""
    agent1, agent2 = two_agents

    # Agent 1 广播
    ts = time.time()
    with db.open_db(agent1) as conn:
        conn.execute("""
            INSERT INTO outbox (msg_id, ts, ts_str, to_id, content, send_deadline)
            VALUES ('msg002', ?, ?, ?, ?, ?)
        """, (ts, "12:01:00", "all", "broadcast message", ts + 2))

    # 模拟 Leader 搬运
    online = db.scan_online_agents()
    process_one_agent(agent1, online)

    # Agent 2 应该收到消息（Agent 1 不会发给自己）
    with db.open_db(agent2) as conn:
        rows = conn.execute("SELECT * FROM inbox").fetchall()
        assert len(rows) == 1
        assert rows[0]["content"] == "broadcast message"

def test_status_request(two_agents):
    """测试 get_status"""
    agent1, agent2 = two_agents

    # Agent 1 请求状态
    with db.open_db(agent1) as conn:
        conn.execute("UPDATE self_state SET status_request=1 WHERE key='main'")

    # 模拟 Leader 处理
    online = db.scan_online_agents()
    process_one_agent(agent1, online)

    # 检查结果
    with db.open_db(agent1) as conn:
        row = conn.execute(
            "SELECT result FROM status_result WHERE key='main'"
        ).fetchone()
        assert row is not None
        result = row["result"]
        # 应该包含两个 Agent
        assert f"Agent {agent1}" in result
        assert f"Agent {agent2}" in result
```

**Step 2: 运行测试**

```bash
cd C:\ccbridge
pytest tests/test_v12_e2e.py -v
```

**Step 3: 提交**

```bash
git add tests/test_v12_e2e.py
git commit -m "test(v12): add end-to-end integration tests"
```

---

## Task 11: 创建迁移文档

**Files:**
- Create: `C:\ccbridge\docs\v12-migration-guide.md`

**Step 1: 创建迁移指南**

```markdown
# CCBridge v12 迁移指南

## 概述

v12 版本从共享数据库架构迁移到每个 Agent 独立数据库架构，彻底消除并发锁竞争问题。

## 变更摘要

| 对比项 | v11 (旧版) | v12 (新版) |
|--------|-----------|-----------|
| 数据库文件 | 2 个共享文件 (bridge_state_*.db, bridge_msg_*.db) | 每 Agent 1 个独立文件 (bridge_agent_*.db) |
| 并发控制 | 多 Agent 抢同一个 DB 锁 | 无竞争，每个 Agent 只访问自己的 DB |
| Leader 选举 | 基于 lease 的复杂选举 | ID 最小的在线 Agent 即为 Leader |
| 消息传递 | 写共享 messages 表 | 写自己的 outbox，Leader 搬运到目标 inbox |

## AI 无感迁移

**MCP 工具接口完全不变：**
- `get_status()` - 查询在线状态
- `send(to, content)` - 发送消息
- `recv(wait_seconds)` - 接收消息

**返回格式完全不变。**

## 手动迁移步骤

1. **备份旧数据（可选）**
   ```bash
   # 旧数据位置
   C:/mcp_msg_pool/bridge_state_v11.db
   C:/mcp_msg_pool/bridge_msg_v11.db

   # 备份
   cp bridge_state_v11.db bridge_state_v11.backup
   cp bridge_msg_v11.db bridge_msg_v11.backup
   ```

2. **切换到 v12**
   ```bash
   # 方式 1：使用新入口
   python bridge_v12_main.py

   # 方式 2：修改 MCP 配置
   # 将 bridge.py 改为 bridge_v12_main.py
   ```

3. **验证**
   - 启动多个 Agent，确认能互相发现
   - 测试 send/recv 功能
   - 测试 get_status 显示正确

## 清理旧数据（确认 v12 正常后）

```bash
# 删除旧数据库
rm C:/mcp_msg_pool/bridge_state_v11.db
rm C:/mcp_msg_pool/bridge_msg_v11.db
```

## 新数据库位置

```
C:/mcp_msg_pool/
├── bridge_agent_001.db
├── bridge_agent_788.db
├── bridge_agent_869.db
└── ...
```

## 故障排查

### 问题：Agent 无法互相发现

**原因：** DB_ROOT 路径不一致

**解决：** 确保所有 Agent 使用相同的 `C:/mcp_msg_pool` 路径

### 问题：消息发送后收不到

**原因：** Leader 未正常运行

**解决：**
1. 检查是否有 Agent 的 ID 最小（Leader）
2. 检查 Leader 的后台维护线程是否运行

### 问题：ID 用完

**现象：** RuntimeError: ID pool exhausted

**原因：** 999 个 ID 都被占用

**解决：** 清理过期 Agent 的数据库文件
```bash
# 删除超过 1 小时未更新的数据库
find C:/mcp_msg_pool -name "bridge_agent_*.db" -mtime +1 -delete
```
```

**Step 2: 提交**

```bash
git add docs/v12-migration-guide.md
git commit -m "docs(v12): add migration guide from v11 to v12"
```

---

## Task 12: 性能基准测试

**Files:**
- Create: `C:\ccbridge\tests\test_v12_benchmark.py`

**Step 1: 创建性能测试**

```python
# C:\ccbridge\tests\test_v12_benchmark.py
import pytest
import time
import tempfile
import shutil
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parent.parent))

from bridge_v12 import db
from bridge_v12.leader import process_one_agent

@pytest.fixture
def many_agents(temp_db_root):
    """创建多个 Agent 用于性能测试"""
    agent_count = 50
    agents = []

    for i in range(agent_count):
        agent_id = f"{i+1:03d}"
        db.init_db(agent_id, 3000 + i, f"bench-host-{i}", f"/bench/path-{i}")
        agents.append(agent_id)

    yield agents

    # 清理
    for agent_id in agents:
        db_path = db.DB_ROOT / f"bridge_agent_{agent_id}.db"
        db_path.unlink(missing_ok=True)

def test_scan_performance(many_agents):
    """测试扫描性能"""
    start = time.time()
    online = db.scan_online_agents()
    elapsed = time.time() - start

    assert len(online) == 50
    assert elapsed < 0.5  # 扫描 50 个 Agent 应该在 500ms 内

def test_leader_cycle_performance(many_agents):
    """测试 Leader 周期性能"""
    online = db.scan_online_agents()

    start = time.time()
    for agent_id in online[:10]:  # 只测试前 10 个
        process_one_agent(agent_id, online)
    elapsed = time.time() - start

    assert elapsed < 1.0  # 处理 10 个 Agent 应该在 1 秒内

def test_send_throughput(temp_db_root):
    """测试发送吞吐量"""
    # 创建 2 个 Agent
    agent1 = db.claim_id()
    db.init_db(agent1, 4001, "send-host-1", "/send/path-1")

    agent2 = db.claim_id()
    db.init_db(agent2, 4002, "send-host-2", "/send/path-2")

    # 发送 100 条消息
    msg_count = 100
    ts = time.time()

    with db.open_db(agent1) as conn:
        for i in range(msg_count):
            conn.execute("""
                INSERT INTO outbox (msg_id, ts, ts_str, to_id, content, send_deadline)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (f"msg{i:03d}", ts + i * 0.001, "12:00:00", agent2, f"message {i}", ts + 2))

    # Leader 搬运
    online = [agent1, agent2]
    start = time.time()
    process_one_agent(agent1, online)
    elapsed = time.time() - start

    # 验证
    with db.open_db(agent2) as conn:
        count = conn.execute("SELECT COUNT(*) FROM inbox").fetchone()[0]
        assert count == msg_count

    # 性能检查
    assert elapsed < 2.0  # 搬运 100 条消息应该在 2 秒内
```

**Step 2: 运行性能测试**

```bash
cd C:\ccbridge
pytest tests/test_v12_benchmark.py -v
```

**Step 3: 提交**

```bash
git add tests/test_v12_benchmark.py
git commit -m "test(v12): add performance benchmarks"
```

---

## 完成清单

- [ ] Task 1: 创建数据库模块 (db.py)
- [ ] Task 2: 创建会话管理模块 (session.py)
- [ ] Task 3: 创建 Leader 维护模块 (leader.py)
- [ ] Task 4: 创建后台维护循环 (maintenance.py)
- [ ] Task 5: 创建消息处理模块 (messaging.py)
- [ ] Task 6: 创建 MCP 工具 (tools.py)
- [ ] Task 7: 创建包初始化文件
- [ ] Task 8: 创建入口脚本
- [ ] Task 9: 测试基本功能
- [ ] Task 10: 端到端测试
- [ ] Task 11: 创建迁移文档
- [ ] Task 12: 性能基准测试

---

**预计总时间:** 2-3 小时

**关键里程碑:**
1. Task 1-4: 核心架构完成（约 1 小时）
2. Task 5-8: MCP 工具完成（约 30 分钟）
3. Task 9-12: 测试和文档（约 1 小时）
