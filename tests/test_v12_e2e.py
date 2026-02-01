# C:\ccbridge\tests\test_v12_e2e.py
import pytest
import time
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parent.parent))

from bridge_v12 import db
from bridge_v12.leader import process_one_agent

@pytest.fixture
def temp_db_root():
    """临时数据库根目录"""
    import tempfile
    import shutil

    original_root = db.DB_ROOT
    temp_dir = tempfile.mkdtemp()
    db.DB_ROOT = Path(temp_dir)
    db.ensure_db_root()

    yield temp_dir

    shutil.rmtree(temp_dir, ignore_errors=True)
    db.DB_ROOT = original_root

@pytest.fixture
def two_agents(temp_db_root):
    """创建两个在线 Agent"""
    agents = []
    for i in range(2):
        agent_id = db.claim_id()
        db.init_db(agent_id, 2000 + i, f"test-host-{i}", f"/test/path-{i}")
        agents.append(agent_id)

    yield agents

    for agent_id in agents:
        db_path = db.DB_ROOT / f"bridge_agent_{agent_id}.db"
        db_path.unlink(missing_ok=True)

def test_send_and_receive(two_agents):
    """测试发送和接收消息"""
    agent1, agent2 = two_agents

    ts = time.time()
    with db.open_db(agent1) as conn:
        conn.execute("""
            INSERT INTO outbox (msg_id, ts, ts_str, to_id, content, send_deadline, state)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, ("msg001", ts, "12:00:00", agent2, "hello from agent1", ts + 2, "pending"))
        conn.commit()

    online = db.scan_online_agents()
    process_one_agent(agent1, online)

    with db.open_db(agent2) as conn:
        rows = conn.execute("SELECT * FROM inbox").fetchall()
        assert len(rows) == 1
        assert rows[0]["from_id"] == agent1
        assert rows[0]["content"] == "hello from agent1"

    with db.open_db(agent1) as conn:
        rows = conn.execute("SELECT * FROM outbox").fetchall()
        assert len(rows) == 0

def test_broadcast(two_agents):
    """测试广播消息"""
    agent1, agent2 = two_agents

    ts = time.time()
    with db.open_db(agent1) as conn:
        conn.execute("""
            INSERT INTO outbox (msg_id, ts, ts_str, to_id, content, send_deadline, state)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, ("msg002", ts, "12:01:00", "all", "broadcast message", ts + 2, "pending"))
        conn.commit()

    online = db.scan_online_agents()
    process_one_agent(agent1, online)

    with db.open_db(agent2) as conn:
        rows = conn.execute("SELECT * FROM inbox").fetchall()
        assert len(rows) == 1
        assert rows[0]["content"] == "broadcast message"

def test_status_request(two_agents):
    """测试 get_status"""
    agent1, agent2 = two_agents

    with db.open_db(agent1) as conn:
        conn.execute("UPDATE self_state SET status_request=1 WHERE key='main'")
        conn.commit()

    online = db.scan_online_agents()
    process_one_agent(agent1, online)

    with db.open_db(agent1) as conn:
        row = conn.execute("SELECT result FROM status_result WHERE key='main'").fetchone()
        assert row is not None
        result = row["result"]
        assert f"Agent {agent1}" in result
        assert f"Agent {agent2}" in result
