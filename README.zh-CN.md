# AI 群聊工作群（MCP）— Bridge MCP

一个 **本地优先** 的 SQLite 消息总线，用来搭建多 Agent 协作"工作群"。
**只有 3 个工具：** `get_status()` / `send(to, content)` / `recv(wait_seconds)`
不需要额外服务器（不需要 Redis / Web 服务），像"AI 微信工作群"一样组织协作。

**引擎版本：** v9_stable（后台心跳 + PID 清理 + Lease 收件） | **发布版本：** [v9.0.0](https://github.com/y2ambition-ai/ai-workgroup-mcp/releases/tag/v9.0.0)
**技术细节：** `docs/ENGINE_v9_STABLE.md`

快速入口：`PLAYBOOK.zh-CN.md` | `PLAYBOOK.md` | `PROMPT_GLOBAL.md` | `examples/` | `docs/ENGINE_v9_STABLE.md`

---

## 安装

需要 Python 3.x

```bash
pip install mcp
claude mcp add bridge --scope=user "python" "C:/ccbridge/bridge.py"
```

Claude Desktop 也可通过客户端配置文件接入（JSON 示例见 `docs/ENGINE_v9_STABLE.md`）。

---

## 三终端验收流程（推荐）

1）开 3 个终端 / 3 个 Agent，都注册 Bridge MCP

2）1号、2号 Agent：先说一句"持续监听；收到消息立刻回复；回复完继续监听"，然后执行：

```bash
recv(86400)  # 你也可以改成任意时长
```

3）3号（主管）：

- 先 `get_status()` 复制真实员工 ID
- **群聊测试：**
  ```bash
  send("all", "测试：回复你的ID+一句状态，然后继续监听。")
  recv(30)  # 没人回就再 recv(30) 一次
  ```
- **私聊验收**（用 `get_status` 看到的真实 ID）：
  ```bash
  send("<员工1ID>", "给主管回一句状态，然后继续监听。")
  send("<员工2ID>", "给主管回一句状态，然后继续监听。")
  ```
- 主管：`recv(60)`

一切正常——恭喜你，你的 AI 团队诞生了。

---

## 核心优势

- **工作群式近实时**（待命监听即可快速响应），不是邮箱式等待
- **极轻量：** 一个 MCP + 一个 SQLite 文件
- **学习成本低：** 只有 3 个工具，输出稳定
- **扩展性强（DB 即 API）：** 外置脚本写入 SQLite 就能当 producer/bot
- **自适应上下线：** 后台心跳 + 清理，在线列表更干净

---

## 消息语义（v9_stable）

v9_stable 用 Lease 队列：

1. 消息默认 `queued`
2. 接收端 lease 成 `inflight`
3. 成功 `recv` 后 ACK（删除）
4. 若中途崩溃/中止未 ACK，lease 释放/过期后消息回到 `queued`（不丢）

---

## 数据库路径（默认 C 盘）

**默认：** `C:\mcp_msg_pool\bridge_v9_stable.db`

**兜底：** `C:\Users\Public\mcp_msg_pool\bridge_v9_stable.db`

**修改：** 编辑 `bridge.py` 的 `PREFERRED_ROOT` / `FALLBACK_ROOT`

---

## 联系方式 / 维护说明

**微信：** vvvykvvv
**邮箱：** 84927052@qq.com

我不是程序员，也不太会手动用 GitHub，大多数更新由 AI 工具协助完成。
建议优先提 Issues/Discussions，我会让 AI 定期阅读整理。

**署名（如作为底层使用）：** Powered by Bridge MCP (by vvvykvvv)
