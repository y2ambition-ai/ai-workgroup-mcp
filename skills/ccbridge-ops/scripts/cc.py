import time
import sys
import os
import json
import ctypes
import psutil
import pyautogui
import pyperclip
from pathlib import Path
import traceback

# ================= 配置区 =================
POLL_INTERVAL = 60       
MAX_NORMAL_TIME = 1200   

WORKER_PROMPT = """
[SYSTEM_GUARD] ⚠️ 状态重置指令
监测到你长时间脱离 Auto 监听模式。
1. 若任务未完成：继续执行。
2. 若任务已完成：立即调用 `recv()` 待命。
3. 若已掉线：立即恢复 Auto Mode。
"""

LEADER_PROMPT = """
[SYSTEM_GUARD] 🛡️ 交付进程守护
Leader，检测到管理进程长时间静默。
1. 【若项目未完结】：请立即恢复 Leader 身份，继续推进。
2. 【若正在等待 Worker】：请忽略本消息。
3. 【若刚完成交付】：请生成总结报告并归档。
4. 【若已交付且一切正常】：请直接忽略本消息，保持静默。
"""
# =========================================

user32 = ctypes.windll.user32
normal_state_tracker = {}

def get_pool_root():
    candidates = [
        Path(os.environ.get("CCBRIDGE_POOL", "")),
        Path("C:/mcp_msg_pool"),
        Path("C:/Users/Public/mcp_msg_pool")
    ]
    for p in candidates:
        if p and p.exists(): return p
    return Path("C:/mcp_msg_pool")

def get_hwnds_for_pid(pid):
    def callback(hwnd, hwnds):
        if user32.IsWindowVisible(hwnd) and user32.IsWindowEnabled(hwnd):
            try:
                _, found_pid = user32.GetWindowThreadProcessId(hwnd, ctypes.byref(ctypes.c_ulong()))
                if found_pid == pid:
                    hwnds.append(hwnd)
            except: pass
        return True
    hwnds = []
    try:
        user32.EnumWindows(ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_int, ctypes.py_object)(callback), hwnds)
    except: pass
    return hwnds

def activate_and_paste(pid, name, prompt_text):
    hwnds = get_hwnds_for_pid(pid)
    if not hwnds:
        try:
            parent = psutil.Process(pid).parent()
            if parent: hwnds = get_hwnds_for_pid(parent.pid)
        except: pass
    
    if not hwnds:
        print(f"      -> ❌ 无法定位窗口 (PID {pid})")
        return

    hwnd = hwnds[0]
    try:
        print(f"      -> ⚡ 激活窗口 [{name}]...")
        if user32.IsIconic(hwnd): user32.ShowWindow(hwnd, 9)
        user32.SetForegroundWindow(hwnd)
        time.sleep(0.5)
        pyperclip.copy(prompt_text.strip())
        pyautogui.hotkey('ctrl', 'v')
        time.sleep(0.2)
        pyautogui.press('enter')
        print(f"      -> ✅ 指令已发送")
    except Exception as e:
        print(f"      -> ❌ 发送失败 (可能是权限不足): {e}")

def smart_patrol():
    pool_root = get_pool_root()
    print(f"[SmartPatrol] Started (Admin: {ctypes.windll.shell32.IsUserAnAdmin() == 1})")
    print(f"[Monitor Dir] {pool_root}")
    
    # 权限警告，但不退出
    if not ctypes.windll.shell32.IsUserAnAdmin():
        print("[WARNING] No admin privileges.")
        print("    If Agent wake-up fails, try running as administrator.")
    
    print("=" * 60)

    while True:
        try:
            now = time.time()
            current_agents = []
            if pool_root.exists():
                for p in pool_root.iterdir():
                    if p.is_dir() and (p / "heartbeat.json").exists():
                        current_agents.append(p.name)
                        try:
                            hb_path = p / "heartbeat.json"
                            data = json.loads(hb_path.read_text(encoding='utf-8'))
                            pid = data.get('pid')
                            state = data.get('state', 'UNKNOWN')
                            
                            if state == "WAITING":
                                if p.name in normal_state_tracker:
                                    del normal_state_tracker[p.name]
                                print(f"[{time.strftime('%H:%M')}] [AUTO] {p.name:<15} | Waiting")
                            else:
                                if p.name not in normal_state_tracker:
                                    normal_state_tracker[p.name] = now
                                duration_min = int((now - normal_state_tracker[p.name]) / 60)
                                print(f"[{time.strftime('%H:%M')}] [WORK] {p.name:<15} | Active: {duration_min}m", end="")

                                if (now - normal_state_tracker[p.name]) > MAX_NORMAL_TIME:
                                    print(f" -> WAKE UP!")
                                    if "leader" in p.name.lower():
                                        activate_and_paste(pid, p.name, LEADER_PROMPT)
                                    else:
                                        activate_and_paste(pid, p.name, WORKER_PROMPT)
                                    normal_state_tracker[p.name] = now
                                else:
                                    print("")
                        except Exception: pass

            for name in list(normal_state_tracker.keys()):
                if name not in current_agents: del normal_state_tracker[name]

            print("-" * 30)
            time.sleep(POLL_INTERVAL)
            
        except KeyboardInterrupt:
            break
        except Exception as e:
            print(f"循环报错: {e}")
            time.sleep(POLL_INTERVAL)

if __name__ == "__main__":
    try:
        smart_patrol()
    except Exception as e:
        print("\n[FATAL ERROR]:")
        traceback.print_exc()
    finally:
        # === 终极防闪退机制 ===
        print("\n[Program Ended] Press Enter to close window...")
        input()
