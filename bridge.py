import os, sys, time, io, uuid, sqlite3, random, asyncio
from pathlib import Path
from mcp.server.fastmcp import FastMCP
from contextlib import contextmanager
from collections import defaultdict

# --- 1. 基础配置 ---
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
mcp = FastMCP("RootBridge")

# --- 2. 策略配置 ---
HEARTBEAT_TTL = 300       # 5分钟掉线
BROADCAST_TTL = 1800      # 广播保留30分钟
DIRECT_MSG_TTL = 86400    # 私信保留24小时
MAINTENANCE_INTERVAL = 10 # 10秒维护一次
MAX_BATCH_SIZE = 5000     # 单批最大字符数

# recv 调度（不改变工具用法，只降低阻塞/锁竞争/CPU）
RECV_TICK = 0.25          # 取消响应速度（越小越"灵敏"）
RECV_DB_POLL_EVERY = 2.0  # 查消息频率
RECV_MAINT_EVERY = 10.0   # 心跳/清理频率

# 版本配置（重要：更新 schema 时必须修改此版本号！）
BRIDGE_DB_VERSION = "v4"
BRIDGE_DB_FILENAME = f"bridge_{BRIDGE_DB_VERSION}.db"

# 路径配置
PREFERRED_ROOT = Path("C:/mcp_msg_pool")
FALLBACK_ROOT = Path("C:/Users/Public/mcp_msg_pool")

# 全局变量
SESSION_NAME = None
SESSION_PID = os.getppid()
LAST_ACTIVE_TS = 0.0  # 最后一次活跃时间戳（用于自动取消旧任务）

def log(msg):
    ts = time.strftime("%H:%M:%S")
    print(f"[{ts}] [BRIDGE] {msg}", file=sys.stderr)

# --- 版本检测与池子管理 ---

def _cleanup_pool():
    """删除整个池子目录（版本不匹配时调用）"""
    import shutil
    for root in [PREFERRED_ROOT, FALLBACK_ROOT]:
        if root.exists():
            try:
                shutil.rmtree(root)
                log(f"Cleaned old pool: {root}")
            except Exception as e:
                log(f"Cleanup failed: {e}")

def _validate_or_rebuild_pool():
    """
    启动时验证数据库版本，不匹配则重建整个池子

    逻辑：
    1. 检查池子目录是否存在
    2. 存在 -> 检查数据库文件名是否匹配当前版本
    3. 不匹配 -> 删除整个池子，让 get_db_path() 重建
    """
    for root in [PREFERRED_ROOT, FALLBACK_ROOT]:
        if root.exists():
            existing_dbs = list(root.glob("bridge_*.db"))
            if existing_dbs:
                # 有数据库文件，检查版本
                if existing_dbs[0].name != BRIDGE_DB_FILENAME:
                    log(f"Version mismatch: found {existing_dbs[0].name}, expecting {BRIDGE_DB_FILENAME}")
                    _cleanup_pool()
                    break

def get_db_path():
    # 先验证版本，不匹配则清理旧池子
    _validate_or_rebuild_pool()

    try:
        PREFERRED_ROOT.mkdir(parents=True, exist_ok=True)
        (PREFERRED_ROOT / ".perm").touch(); (PREFERRED_ROOT / ".perm").unlink()
        return PREFERRED_ROOT / BRIDGE_DB_FILENAME
    except:
        FALLBACK_ROOT.mkdir(parents=True, exist_ok=True)
        return FALLBACK_ROOT / BRIDGE_DB_FILENAME

DB_PATH = get_db_path()

# --- 3. 数据库核心层 ---

@contextmanager
def get_db():
    conn = None
    try:
        conn = sqlite3.connect(str(DB_PATH), timeout=10.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=5000;")
        yield conn
    except sqlite3.OperationalError as e:
        log(f"DB Busy/Locked: {e}")
        raise
    except Exception as e:
        log(f"DB Error: {e}")
        raise
    finally:
        if conn: conn.close()

def init_db():
    with get_db() as conn:
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")

        # Peers 表 (加 cwd 字段)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS peers (
                id TEXT PRIMARY KEY,
                pid INTEGER,
                last_seen REAL,
                cwd TEXT
            )
        """)

        # Messages 表
        conn.execute("""
            CREATE TABLE IF NOT EXISTS messages (
                msg_id TEXT PRIMARY KEY,
                ts REAL,
                ts_str TEXT,
                from_user TEXT,
                to_user TEXT,
                content TEXT,
                is_broadcast INTEGER
            )
        """)

        # State 表
        conn.execute("""
            CREATE TABLE IF NOT EXISTS state (
                agent_id TEXT PRIMARY KEY,
                last_broadcast_ts REAL
            )
        """)
        # 索引：减少查询与持锁时间（兼容旧库，幂等）
        conn.execute("CREATE INDEX IF NOT EXISTS idx_peers_last_seen ON peers(last_seen);")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_messages_to_user ON messages(to_user, is_broadcast, ts);")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_messages_broadcast ON messages(is_broadcast, to_user, ts);")

        conn.commit()

try:
    init_db()
except Exception as e:
    log(f"Init Failed: {e}")

# --- 4. 业务逻辑层 ---

def _generate_id() -> str:
    """随机生成 3 位数字 ID (001-999)，最多尝试 5000 次"""
    for _ in range(5000):
        candidate_id = f"{random.randint(1, 999):03d}"
        # 检查是否已使用
        with get_db() as conn:
            cur = conn.execute("SELECT 1 FROM peers WHERE id = ?", (candidate_id,))
            if not cur.fetchone():
                return candidate_id
    raise RuntimeError("无法生成唯一 ID：所有 ID 都被占用")

def _update_heartbeat(name: str, pid: int):
    cwd = os.getcwd()
    with get_db() as conn:
        conn.execute("""
            INSERT OR REPLACE INTO peers (id, pid, last_seen, cwd)
            VALUES (?, ?, ?, ?)
        """, (name, pid, time.time(), cwd))
        conn.commit()

def get_session():
    global SESSION_NAME
    if not SESSION_NAME:
        SESSION_NAME = _generate_id()
        _update_heartbeat(SESSION_NAME, SESSION_PID)
    return SESSION_NAME, SESSION_PID

def _mark_active():
    """标记当前活跃，用于取消旧任务"""
    global LAST_ACTIVE_TS
    LAST_ACTIVE_TS = time.time()

# 启动时自动注册
_auto_name, _auto_pid = get_session()

def _get_active_peers(my_name: str):
    limit = time.time() - HEARTBEAT_TTL
    with get_db() as conn:
        cursor = conn.execute("SELECT id, cwd FROM peers WHERE last_seen > ? ORDER BY id", (limit,))
        peers = [{"id": row['id'], "cwd": row['cwd']} for row in cursor.fetchall()]
    return peers

def _send_db(from_user, to_user, content, is_broadcast):
    msg_id = uuid.uuid4().hex[:8]
    ts = time.time()
    ts_str = time.strftime("%H:%M:%S")

    with get_db() as conn:
        conn.execute("""
            INSERT INTO messages (msg_id, ts, ts_str, from_user, to_user, content, is_broadcast)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (msg_id, ts, ts_str, from_user, to_user, content, 1 if is_broadcast else 0))
        conn.commit()
    return msg_id

def _collect_db(my_name):
    """Collect messages for me, then delete direct messages and advance broadcast cursor.

    关键改动（不影响 AI 工具用法）：
    - 不在热路径里反复设置 WAL
    - 不再一上来 BEGIN IMMEDIATE 抢写锁（读为主，写尽量短）
    - 只在确实需要 delete/update 时才开启短写事务
    """
    msgs = []
    conn = None

    try:
        conn = sqlite3.connect(str(DB_PATH), timeout=10.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=5000;")

        # ---- read state (no write lock) ----
        cur = conn.execute("SELECT last_broadcast_ts FROM state WHERE agent_id = ?", (my_name,))
        row = cur.fetchone()
        last_bc_ts = row["last_broadcast_ts"] if row else 0.0

        now = time.time()
        bc_limit = now - BROADCAST_TTL
        new_max_bc_ts = last_bc_ts

        # ---- read direct ----
        direct_rows = conn.execute(
            """
            SELECT * FROM messages
            WHERE to_user = ? AND is_broadcast = 0
            ORDER BY ts ASC
            """,
            (my_name,),
        ).fetchall()

        direct_ids = []
        for r in direct_rows:
            msgs.append(dict(r))
            direct_ids.append(r["msg_id"])

        # ---- read broadcast ----
        bc_rows = conn.execute(
            """
            SELECT * FROM messages
            WHERE is_broadcast = 1
              AND to_user = ?
              AND from_user != ?
              AND ts > ?
              AND ts > ?
            ORDER BY ts ASC
            """,
            (my_name, my_name, last_bc_ts, bc_limit),
        ).fetchall()

        for r in bc_rows:
            msgs.append(dict(r))
            if r["ts"] > new_max_bc_ts:
                new_max_bc_ts = r["ts"]

        # ---- write only if needed (short window) ----
        if direct_ids or (new_max_bc_ts > last_bc_ts):
            conn.execute("BEGIN")
            if direct_ids:
                placeholders = ",".join("?" * len(direct_ids))
                conn.execute(f"DELETE FROM messages WHERE msg_id IN ({placeholders})", direct_ids)

            if new_max_bc_ts > last_bc_ts:
                conn.execute(
                    "INSERT OR REPLACE INTO state (agent_id, last_broadcast_ts) VALUES (?, ?)",
                    (my_name, new_max_bc_ts),
                )
            conn.commit()

    except Exception as e:
        if conn:
            try:
                conn.rollback()
            except:
                pass
        log(f"_collect_db error: {e}")
        raise
    finally:
        if conn:
            conn.close()

    return msgs

def _maintenance_db(my_name, my_pid):
    now = time.time()
    _update_heartbeat(my_name, my_pid)

    if hash(my_name) % 2 == 0:
        try:
            with get_db() as conn:
                conn.execute("DELETE FROM peers WHERE last_seen < ?", (now - HEARTBEAT_TTL,))
                conn.execute("DELETE FROM messages WHERE is_broadcast = 1 AND ts < ?", (now - BROADCAST_TTL,))
                conn.execute("DELETE FROM messages WHERE is_broadcast = 0 AND ts < ?", (now - DIRECT_MSG_TTL,))
                conn.commit()
        except Exception as e:
            log(f"Maintenance error: {e}")

def _format_msgs_grouped(msgs, remaining_count=0):
    """按发送者分组格式化消息"""
    if not msgs:
        return "No messages."

    # 按发送者分组
    grouped = defaultdict(list)
    for m in msgs:
        grouped[m['from_user']].append(m)

    # 按时间戳排序所有组
    sorted_senders = sorted(grouped.keys(), key=lambda x: min(m['ts'] for m in grouped[x]))

    lines = [f"=== {len(msgs)} messages from {len(grouped)} agent(s) ===\n"]

    for sender in sorted_senders:
        sender_msgs = grouped[sender]
        lines.append(f"[{sender}] - {len(sender_msgs)} message(s)")
        for m in sender_msgs:
            lines.append(f"  {m['ts_str']} {m['content']}")
        lines.append("")

    if remaining_count > 0:
        lines.append(f"({remaining_count} more message(s). Call recv() again to continue)")

    return "\n".join(lines)

def _estimate_size(msgs):
    """估算消息总字符数"""
    total = 0
    for m in msgs:
        total += len(m['content']) + 50  # 内容 + 元数据估算
    return total

# --- 5. MCP Tools ---

@mcp.tool()
def get_status() -> str:
    """Show your ID and online agents."""
    _mark_active()
    name, pid = get_session()
    _maintenance_db(name, pid)
    peers = _get_active_peers(name)

    lines = [f"YOU: {name} ({os.getcwd()})", f"ONLINE: {len(peers)}"]
    for p in peers:
        marker = " - YOU" if p['id'] == name else ""
        lines.append(f"  {p['id']} ({p['cwd']}){marker}")

    return "\n".join(lines)

@mcp.tool()
def send(to: str, content: str) -> str:
    """Send message. to: 'all' or '001,003'."""
    _mark_active()
    name, pid = get_session()
    _maintenance_db(name, pid)

    recipients = [r.strip() for r in to.split(",")]
    peers = _get_active_peers(name)
    peer_ids = [p['id'] for p in peers]

    if "all" in [r.lower() for r in recipients]:
        recipients = peer_ids
        recipients = [r for r in recipients if r != name]
        if not recipients:
            return "No other agents online"
        is_broadcast = True
    else:
        is_broadcast = False
        if name in recipients:
            return f"Error: Cannot send message to yourself"
        for rec in recipients:
            if rec not in peer_ids:
                return f"Error: Agent '{rec}' not found online"

    try:
        first_msg_id = None
        for rec in recipients:
            msg_id = _send_db(name, rec, content, is_broadcast)
            if not first_msg_id:
                first_msg_id = msg_id
        return f"Sent (ID: {first_msg_id}, to {len(recipients)} recipient(s))"
    except Exception as e:
        return f"DB Error: {e}"

@mcp.tool()
async def recv(wait_seconds: int = 86400) -> str:
    """Receive messages. Wait up to wait_seconds; cancelled if another tool is called."""
    _mark_active()
    name, pid = get_session()

    start = time.monotonic()
    my_start_ts = LAST_ACTIVE_TS

    # 消息处理函数（沿用原输出，AI 不用重新学）
    def process_messages(messages):
        if not messages:
            return None
        total_size = _estimate_size(messages)
        if total_size <= MAX_BATCH_SIZE:
            return _format_msgs_grouped(messages)
        # 分批处理
        current_size = 0
        batch_msgs = []
        for m in messages:
            msg_size = len(m['content']) + 50
            if current_size + msg_size > MAX_BATCH_SIZE and batch_msgs:
                break
            batch_msgs.append(m)
            current_size += msg_size
        return _format_msgs_grouped(batch_msgs, len(messages) - len(batch_msgs))

    # 初始检查（DB IO 放线程池，避免阻塞）
    try:
        msgs = await asyncio.to_thread(_collect_db, name)
        result = process_messages(msgs)
        if result:
            return result
    except Exception as e:
        log(f"Initial check error: {e}")
        return f"DB Read Error: {e}"

    if wait_seconds <= 0:
        return "No new messages."

    next_poll_at = time.monotonic() + RECV_DB_POLL_EVERY
    next_maint_at = time.monotonic() + RECV_MAINT_EVERY

    # 长等待循环（不占用事件循环）
    while True:
        # 取消优先级最高
        if LAST_ACTIVE_TS != my_start_ts:
            return "Cancelled by new task."

        elapsed = time.monotonic() - start
        if elapsed >= wait_seconds:
            return f"Timeout ({wait_seconds}s). No new messages."

        now_m = time.monotonic()

        # 维护（心跳/清理）
        if now_m >= next_maint_at:
            next_maint_at = now_m + RECV_MAINT_EVERY
            try:
                await asyncio.to_thread(_maintenance_db, name, pid)
            except Exception as e:
                log(f"Maintenance error: {e}")

        # 拉消息
        if now_m >= next_poll_at:
            next_poll_at = now_m + RECV_DB_POLL_EVERY
            try:
                msgs = await asyncio.to_thread(_collect_db, name)
                result = process_messages(msgs)
                if result:
                    return result
            except Exception as e:
                log(f"Recv loop error: {e}")

        await asyncio.sleep(RECV_TICK)

if __name__ == "__main__":
    mcp.run()
