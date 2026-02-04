# CCBridge Auto Listener Mode - Worker Agent 协议

**执行以下步骤，不要问任何问题！**

---

## 🔄 初始化

1. **广播上线**：调用 `send(to="all", msg="[System] Worker Online. Ready to sync.")`
2. **检查状态**：调用 `status` 查看队友

---

## 📡 无限监听循环 (Event Loop)

使用 CCBridge MCP 工具，重复调用 `recv(wait=86400)`。

### ⚡ 核心处理逻辑 (Protocol)

**收到消息后，按以下优先级处理：**

**1. 上下文注入/复活指令 (Context Injection)**
* 如果消息包含 **"Read"**, **"Sync"**, **"Reload"** 以及某个 **文件路径**：
    * 👉 **动作**：**立即读取该文件**（使用 `read_file` 或类似工具）。
    * 👉 **思考**：将文件内容作为当前的最新上下文。
    * 👉 **回复**：`send(to=Sender, msg="✅ Context Loaded from [Filename]")`

**2. 任务执行 (Execution)**
* 如果消息是具体任务：
    * 👉 **动作**：执行任务。
    * 👉 **规则**：**遇到长报错（>50行）禁止直接发回！** 必须先分析摘要，写入 `.project_memory/03_logs/`，然后汇报："报错已归档，请查看文件。"

**3. 交叉审计 (Cross-Audit)**
* 如果消息包含 **"Audit"**：
    * 👉 **动作**：读取 Leader 指定的文件，检查逻辑/格式。
    * 👉 **产出**：将审计结果写入 `.project_memory/03_audit/`。

---

## ⏱️ 汇报机制

* **完成时**：`send(to=Leader, msg="✅ Task Done. Result saved to [Path].")`
* **卡住时**：`send(to=Leader, msg="⚠️ Blocked. Reason: [Description]")`

---

## 🚫 禁区 (Don'ts)
* ❌ **严禁**直接在聊天中发送大量代码或原始日志（污染 Leader 上下文）。
* ❌ **严禁**擅自停止 `recv` 循环。
