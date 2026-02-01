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
    return await asyncio.to_thread(_recv, wait_seconds)

if __name__ == "__main__":
    mcp.run()
