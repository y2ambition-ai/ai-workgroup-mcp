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
def temp_db_root():
    """临时数据库根目录"""
    original_root = db.DB_ROOT
    temp_dir = tempfile.mkdtemp()
    db.DB_ROOT = Path(temp_dir)
    db.ensure_db_root()

    yield temp_dir

    shutil.rmtree(temp_dir, ignore_errors=True)
    db.DB_ROOT = original_root

@pytest.fixture
def many_agents(temp_db_root):
    """创建多个 Agent 用于性能测试"""
    agents = []
    for i in range(50):
        agent_id = f"{i+1:03d}"
        db.init_db(agent_id, 3000 + i, f"bench-host-{i}", f"/bench/path-{i}")
        agents.append(agent_id)

    yield agents

    for agent_id in agents:
        db_path = db.DB_ROOT / f"bridge_agent_{agent_id}.db"
        db_path.unlink(missing_ok=True)

def test_scan_performance(many_agents):
    """测试扫描性能"""
    start = time.time()
    online = db.scan_online_agents()
    elapsed = time.time() - start

    assert len(online) == 50
    assert elapsed < 0.5

def test_leader_cycle_performance(many_agents):
    """测试 Leader 周期性能"""
    online = db.scan_online_agents()

    start = time.time()
    for agent_id in online[:10]:
        process_one_agent(agent_id, online)
    elapsed = time.time() - start

    assert elapsed < 1.0

def test_send_throughput(temp_db_root):
    """测试发送吞吐量"""
    agent1 = db.claim_id()
    db.init_db(agent1, 4001, "send-host-1", "/send/path-1")

    agent2 = db.claim_id()
    db.init_db(agent2, 4002, "send-host-2", "/send/path-2")

    msg_count = 100
    ts = time.time()

    with db.open_db(agent1) as conn:
        for i in range(msg_count):
            conn.execute("""
                INSERT INTO outbox (msg_id, ts, ts_str, to_id, content, send_deadline)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (f"msg{i:03d}", ts + i * 0.001, "12:00:00", agent2, f"message {i}", ts + 2))
        conn.commit()

    online = [agent1, agent2]
    start = time.time()
    # 需要处理两次才能搬运所有 100 条消息（批次大小限制为 50）
    process_one_agent(agent1, online)
    process_one_agent(agent1, online)
    elapsed = time.time() - start

    with db.open_db(agent2) as conn:
        count = conn.execute("SELECT COUNT(*) FROM inbox").fetchone()[0]
        assert count == msg_count

    assert elapsed < 2.0
