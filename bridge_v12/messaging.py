# C:\ccbridge\bridge_v12\messaging.py
import time
import uuid

from .db import open_db
from .session import get_session, mark_active

def send(to: str, content: str) -> str:
    """发送消息给指定 Agent 或所有 Agent"""
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
                        delivered.append(msg_id)
                    else:
                        remaining.append(msg_id)
            except Exception:
                remaining.append(msg_id)

        msg_ids = remaining
        if not msg_ids:
            return f"Sent (to {len(delivered)} agent(s), id={first_short})"

        time.sleep(check_interval)

    # 超时
    if delivered:
        return f"Partially sent (to {len(delivered)}/{len(recipients)} agents, id={first_short})"

    return f"Send timeout after 2s (to {len(recipients)} agents)"

def recv(wait_seconds: int = 86400) -> str:
    """接收消息"""
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

            time.sleep(0.25)
    finally:
        if waiting_marked:
            clear_waiting_mode()

def fetch_inbox_messages() -> list[dict]:
    """获取 inbox 中的所有消息并清空"""
    my_id, _ = get_session()
    try:
        with open_db(my_id) as conn:
            rows = conn.execute("SELECT * FROM inbox ORDER BY ts").fetchall()
            messages = [dict(r) for r in rows]
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

def set_waiting_mode(wait_seconds: int) -> None:
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

def clear_waiting_mode() -> None:
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
