# C:\ccbridge\bridge_v12\session.py
import os
import time
import platform
import threading
from typing import Optional

from .db import init_db, claim_id, open_db

__all__ = ["get_session", "mark_active"]

# Constants
HEARTBEAT_INTERVAL = 10.0

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

def _update_heartbeat() -> None:
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
        # Silently ignore heartbeat update errors - database may be locked
        # This prevents crashes during concurrent access or temporary I/O issues
        pass

def mark_active() -> None:
    """标记当前会话为活跃状态"""
    global LAST_ACTIVE_TS
    LAST_ACTIVE_TS = time.time()

def _heartbeat_loop() -> None:
    """后台心跳循环"""
    while True:
        _update_heartbeat()
        time.sleep(HEARTBEAT_INTERVAL)

def _ensure_background_started() -> None:
    """确保后台线程已启动"""
    global _background_started
    if _background_started:
        return
    with _background_lock:
        if _background_started:
            return
        t = threading.Thread(target=_heartbeat_loop, daemon=True)
        t.start()
        _background_started = True

# Note: Session initialization should be called explicitly by the application:
#   get_session()      # Initialize session and claim ID
#   _ensure_background_started()  # Start heartbeat loop
# These are separated to allow the application to control initialization timing.
