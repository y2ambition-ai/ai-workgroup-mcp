# C:\ccbridge\bridge_v12\maintenance.py
import time
import threading
import random

from .leader import leader_maintenance_cycle
from .session import get_session

# --- 配置 ---
BASE_POLL_INTERVAL = 0.5  # 基础轮询间隔（秒）

def _maintenance_loop() -> None:
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

def ensure_maintenance_started() -> None:
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
