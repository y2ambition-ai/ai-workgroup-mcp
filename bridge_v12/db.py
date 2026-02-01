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
MIN_ID = 1          # Agent ID 范围起始值
MAX_ID = 999        # Agent ID 范围结束值

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
                # Silently ignore connection close errors - connection may be
                # corrupted or the file may have been deleted by another process
                pass

def init_db(agent_id: str, pid: int, hostname: str, cwd: str) -> None:
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
            # 文件损坏，删除重建 - silently handle corrupted database files
            # by removing them and recreating from scratch
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

    Raises:
        RuntimeError: 如果 ID 池耗尽（1-999 全部被占用且未过期）
    """
    ensure_db_root()

    # 主要路径：找第一个文件不存在的 ID
    for candidate_id in range(MIN_ID, MAX_ID + 1):
        cid = f"{candidate_id:03d}"
        db_path = DB_ROOT / f"bridge_agent_{cid}.db"
        if not db_path.exists():
            return cid

    # 极罕见：999 个文件都存在，找最旧的过期 ID
    now = time.time()
    oldest_id = None
    oldest_heartbeat = float('inf')

    for candidate_id in range(MIN_ID, MAX_ID + 1):
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
                # 文件损坏，可以直接复用 - database is corrupted,
                # so it's safe to reuse this ID
                return cid

    if oldest_id:
        # 删除旧文件
        db_path = DB_ROOT / f"bridge_agent_{oldest_id}.db"
        db_path.unlink(missing_ok=True)
        return oldest_id

    raise RuntimeError(f"ID pool exhausted ({MIN_ID}-{MAX_ID} all in use)")

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
            # Silently skip corrupted database files during scan -
            # corrupted databases are ignored and won't appear in results
            continue

    return sorted(online_agents)
