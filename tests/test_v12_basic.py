# C:\ccbridge\tests\test_v12_basic.py
import pytest
import time
import tempfile
import shutil
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parent.parent))

from bridge_v12 import db

@pytest.fixture
def temp_db_root():
    """临时数据库根目录"""
    original_root = db.DB_ROOT
    temp_dir = tempfile.mkdtemp()
    db.DB_ROOT = Path(temp_dir)
    db.ensure_db_root()

    yield temp_dir

    shutil.rmtree(temp_dir, ignore_errors=True)
    db.DB_ROOT = original_root

def test_claim_id(temp_db_root):
    """测试 ID 获取"""
    id1 = db.claim_id()
    assert id1 == "001"

    # 需要删除文件才能获取下一个 ID
    db_path1 = db.DB_ROOT / f"bridge_agent_{id1}.db"
    db_path1.unlink(missing_ok=True)

    id2 = db.claim_id()
    assert id2 == "001"  # 复用已删除的 ID

def test_init_db(temp_db_root):
    """测试数据库初始化"""
    agent_id = db.claim_id()
    db.init_db(agent_id, 12345, "test-host", "/test/path")

    db_path = db.DB_ROOT / f"bridge_agent_{agent_id}.db"
    assert db_path.exists()

    with db.open_db(agent_id) as conn:
        tables = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        table_names = [t["name"] for t in tables]
        assert "self_state" in table_names
        assert "inbox" in table_names
        assert "outbox" in table_names
        assert "status_result" in table_names

def test_scan_online_agents(temp_db_root):
    """测试扫描在线 Agent"""
    agents = []
    for i in range(3):
        agent_id = db.claim_id()
        db.init_db(agent_id, 1000 + i, f"host-{i}", f"/path-{i}")
        agents.append(agent_id)

    online = db.scan_online_agents()
    assert len(online) == 3
    assert set(online) == set(agents)

def test_send_to_all(temp_db_root):
    """测试广播消息"""
    agent1 = db.claim_id()
    db.init_db(agent1, 1001, "host-1", "/path-1")

    agent2 = db.claim_id()
    db.init_db(agent2, 1002, "host-2", "/path-2")

    with db.open_db(agent1) as conn:
        conn.execute("""
            INSERT INTO outbox (msg_id, ts, ts_str, to_id, content, send_deadline, state)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, ("test123", time.time(), "12:00:00", "all", "hello", time.time() + 2, "pending"))
        conn.commit()

    with db.open_db(agent1) as conn:
        rows = conn.execute("SELECT * FROM outbox").fetchall()
        assert len(rows) == 1
        assert rows[0]["to_id"] == "all"
