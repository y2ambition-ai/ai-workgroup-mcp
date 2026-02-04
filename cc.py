#!/usr/bin/env python3
"""
Claude Code Agent Manager (v25 compatible - file-based message pool)

使用方式:
  python cc.py start [directory]          - 在指定目录启动同事（默认当前目录）
  python cc.py kill <agent_id>            - 关闭指定同事
  python cc.py kill agent1,agent2,agent3  - 批量关闭多个同事
"""

import json
import psutil
import subprocess
import sys
import time
import os
from pathlib import Path

# Fix Windows console encoding for emoji output
if sys.platform == 'win32':
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')

try:
    import pyautogui
    import pyperclip
    HAS_PYAUTOGUI = True
except ImportError:
    HAS_PYAUTOGUI = False

# ---------------- 配置 ----------------
# 注意：轮询机制已自适应等待，无需额外等待时间配置

# ---------------- Message Pool discovery (v25) ----------------

DEFAULT_ROOTS = [
    Path("C:/mcp_msg_pool"),
    Path("C:/Users/Public/mcp_msg_pool"),
]

def _auto_find_pool() -> Path | None:
    """查找消息池根目录"""
    for root in DEFAULT_ROOTS:
        if root.exists() and root.is_dir():
            return root
    return None

env_pool = os.environ.get("CCBRIDGE_POOL")
POOL_ROOT = Path(env_pool) if env_pool else (_auto_find_pool() or Path("C:/mcp_msg_pool"))

# ---------------- claude.exe discovery ----------------

CLAUDE_EXE = Path.home() / ".local" / "bin" / "claude.exe"

def _find_claude_exe() -> Path | None:
    global CLAUDE_EXE
    if CLAUDE_EXE.exists():
        return CLAUDE_EXE

    # try PATH
    for p in os.environ.get("PATH", "").split(";"):
        exe = Path(p) / "claude.exe"
        if exe.exists():
            CLAUDE_EXE = exe
            return exe
    return None

# ---------------- process utils ----------------

def find_ancestor_pid(pid: int, target_names: set[str]) -> int | None:
    """walk parent chain to find first ancestor with name in target_names"""
    try:
        p = psutil.Process(pid)
    except Exception:
        return None
    seen = set()
    while True:
        if p.pid in seen:
            return None
        seen.add(p.pid)
        try:
            name = (p.name() or "").lower()
        except Exception:
            name = ""
        if name in target_names:
            return p.pid
        try:
            parent = p.parent()
        except Exception:
            parent = None
        if not parent or parent.pid <= 0:
            return None
        p = parent

# ---------------- Agent utils (file-based, v25) ----------------

def get_all_agents() -> dict[str, dict]:
    """扫描消息池，返回所有 agent 信息 {agent_id: {pid, state, ts}}"""
    agents = {}
    if not POOL_ROOT.exists():
        return agents

    now = time.time()
    for agent_dir in POOL_ROOT.iterdir():
        if not agent_dir.is_dir():
            continue

        # 尝试读取 heartbeat.json 获取 PID
        pid = None
        state = 'NORMAL'
        ts = now
        hb_file = agent_dir / "heartbeat.json"

        if hb_file.exists():
            try:
                with open(hb_file, 'r', encoding='utf-8') as f:
                    hb_data = json.load(f)
                pid = hb_data.get('pid')
                state = hb_data.get('state', 'NORMAL')
                ts = hb_data.get('ts', now)
            except Exception:
                pass

        agents[agent_dir.name] = {
            'pid': pid,
            'state': state,
            'ts': ts,
            'age': now - ts
        }

    return agents

def get_agent_pid(agent_id: str) -> int | None:
    """获取 Agent 的 MCP 进程 PID"""
    agents = get_all_agents()
    if agent_id in agents:
        return agents[agent_id].get('pid')
    return None

def delete_agent_folder(agent_id: str) -> bool:
    """删除 agent 文件夹"""
    agent_dir = POOL_ROOT / agent_id
    if agent_dir.exists():
        try:
            import shutil
            shutil.rmtree(agent_dir)
            return True
        except Exception:
            pass
    return False

# ---------------- commands ----------------

def start_agent(directory=None):
    if directory is None:
        print("错误: 必须指定启动目录")
        print("用法: python cc.py start <directory>")
        print("示例: python cc.py start \"D:\\my_project\"")
        return 1

    # 自动创建目录（如果不存在）
    dir_path = Path(directory)
    if not dir_path.exists():
        print(f"目录不存在，自动创建: {directory}")
        dir_path.mkdir(parents=True, exist_ok=True)
        print(f"已创建目录")

    exe = _find_claude_exe()
    if not exe:
        print("错误: 找不到 claude.exe")
        return 1

    # ========== 第一步：快照当前 agent 数量 ==========
    initial_agents = set(get_all_agents().keys())
    initial_count = len(initial_agents)
    print(f"当前已加载 {initial_count} 个 agent")

    # ========== 第二步：启动新终端 ==========
    print(f"在 {directory} 启动同事...")
    print(f"执行: {exe} --dangerously-skip-permissions")

    process = subprocess.Popen(
        [str(exe), "--dangerously-skip-permissions"],
        cwd=directory,
        creationflags=subprocess.CREATE_NEW_CONSOLE
    )

    print(f"已启动，PID: {process.pid}")

    # ========== 第三步：轮询等待新 agent 出现 ==========
    print("等待 agent 加载 MCP 服务器...")
    max_wait = 60  # 最多等待 60 秒
    start_time = time.time()
    new_agent_id = None

    while time.time() - start_time < max_wait:
        current_agents = set(get_all_agents().keys())
        new_agents = current_agents - initial_agents

        if new_agents:
            # 找到新出现的 agent（静默，不暴露内部ID）
            new_agent_id = list(new_agents)[0]
            break

        time.sleep(0.5)  # 每 0.5 秒检查一次

    if not new_agent_id:
        print("错误: 等待超时，未检测到新 agent")
        print("提示: 可以手动切换到新窗口并输入 /auto")
        return 1

    # ========== 第四步：等待 3 秒后注入 ==========
    time.sleep(3)
    print("开始注入 /auto...")

    if HAS_PYAUTOGUI:
        # 使用 pyautogui 注入
        print("  注入 /auto...")
        import pyperclip
        pyperclip.copy('/auto')
        pyautogui.hotkey('ctrl', 'v')
        time.sleep(0.15)
        pyautogui.press('enter')
        print("  完成！")
    else:
        # 回退到 PowerShell 方式
        print("  注入 /auto (PowerShell 方式)...")
        print("  提示: 安装 pyautogui 可获得更稳定的效果")
        subprocess.run([
            "powershell", "-Command",
            "Set-Clipboard -Value '/auto'; "
            "$wshell = New-Object -ComObject WScript.Shell; "
            "Start-Sleep -Milliseconds 200; "
            "$wshell.SendKeys('^v{ENTER}')"
        ], capture_output=True)

    print(f"完成！同事已进入 /auto 模式")
    # 等待窗口完全准备好（再启动下一个同事前）
    time.sleep(2)
    return 0


def kill_agent(agent_id: str):
    """通过 Agent ID 关闭 Agent

    使用方式:
        python cc.py kill <agent_id>    # 推荐：使用 Agent ID
    """
    agents = get_all_agents()

    if agent_id not in agents:
        print(f"错误: 找不到 Agent {agent_id}")
        print(f"可用的 agents: {', '.join(agents.keys())}")
        return 1

    mcp_pid = agents[agent_id].get('pid')
    if not mcp_pid:
        print(f"Agent {agent_id} 没有 PID 信息，可能已离线")
        delete_agent_folder(agent_id)
        return 0

    # Prefer killing cmd.exe ancestor (closes tab / session)
    cmd_pid = find_ancestor_pid(mcp_pid, {"cmd.exe"})
    kill_pid = cmd_pid or None

    # Fallback: kill claude.exe ancestor if cmd not found
    if not kill_pid:
        kill_pid = find_ancestor_pid(mcp_pid, {"claude.exe"}) or None

    # Last resort: kill mcp_pid's parent
    if not kill_pid:
        try:
            kill_pid = psutil.Process(mcp_pid).ppid()
        except Exception:
            kill_pid = None

    if not kill_pid:
        print(f"Agent {agent_id} 的进程已结束")
        delete_agent_folder(agent_id)
        return 0

    print(f"关闭 Agent {agent_id}...")
    print(f"  mcp_pid: {mcp_pid}")
    print(f"  kill_pid: {kill_pid}")

    # 检查进程是否存在
    if not psutil.pid_exists(kill_pid):
        print(f"进程 {kill_pid} 已关闭（无需操作）")
        delete_agent_folder(agent_id)
        print("记录已清理")
        return 0

    # 执行关闭
    try:
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(kill_pid)],
                      capture_output=True)
    except:
        pass

    # 等待进程真正结束
    print("等待进程结束...")
    for i in range(10):  # 最多等待 1 秒
        time.sleep(0.1)
        if not psutil.pid_exists(kill_pid):
            print("成功！Agent 已关闭")
            delete_agent_folder(agent_id)
            print("记录已清理")
            return 0

    # 如果还在，再检查一次
    if not psutil.pid_exists(kill_pid):
        print("成功！Agent 已关闭")
        delete_agent_folder(agent_id)
        print("记录已清理")
        return 0

    print(f"错误: 进程 {kill_pid} 未能关闭")
    return 1


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return 1

    command = sys.argv[1].lower()

    if command == "start":
        directory = sys.argv[2] if len(sys.argv) > 2 else None
        return start_agent(directory)

    elif command == "kill":
        if len(sys.argv) < 3:
            print("用法: python cc.py kill <agent_id>")
            print("      python cc.py kill agent1,agent2,agent3")
            print("提示: agent_id 可以从 status() MCP 工具获取")
            return 1

        target = sys.argv[2]

        # 支持批量关闭: kill Alice,Bob,Charlie
        if "," in target:
            targets = [t.strip() for t in target.split(",")]
            print(f"批量关闭 {len(targets)} 个 agents...")
            failed = []
            for t in targets:
                if kill_agent(t) != 0:
                    failed.append(t)
            if failed:
                print(f"\n失败: {', '.join(failed)}")
                return 1
            print(f"\n全部完成！")
            return 0

        # 禁用 kill all（防止误关自己）
        if target.lower() == "all":
            print("错误: 'kill all' 已禁用（防止误关自己）")
            print("请使用: python cc.py kill agent1,agent2,agent3")
            agents = get_all_agents()
            print(f"可用的 agents: {', '.join(agents.keys())}")
            return 1

        # 单个关闭
        return kill_agent(target)

    else:
        print(f"未知命令: {command}")
        print(__doc__)
        return 1


if __name__ == "__main__":
    sys.exit(main())
