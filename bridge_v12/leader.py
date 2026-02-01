# C:\ccbridge\bridge_v12\leader.py
import time
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
    for target_id in targets:
        try:
            with open_db(target_id) as conn:
                conn.execute("""
                    INSERT INTO inbox (msg_id, ts, ts_str, from_id, content)
                    VALUES (?, ?, ?, ?, ?)
                """, (msg["msg_id"], msg["ts"], msg["ts_str"], from_id, msg["content"]))
        except Exception:
            # Silently ignore delivery errors - target database may be temporarily locked
            return False

    return True

def process_one_agent(agent_id: str, online_agents: list[str]) -> None:
    """
    Leader 处理单个 Agent 的所有待办事项

    Args:
        agent_id: 要处理的 Agent ID
        online_agents: 当前在线 Agent 列表
    """
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
        # Silently skip agents with database access errors
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
        # Silently ignore cleanup errors - may retry next cycle
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
            # Silently skip agents with database access errors
            continue

    # 排序：自己在前，其他按 ID
    agents_info.sort(key=lambda x: (0, x[1]) if x[0] else (1, x[1]))

    lines = [line for _, _, line in agents_info]
    return "\n".join(lines) if lines else "No active agents."

def leader_maintenance_cycle() -> tuple[bool, int]:
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
