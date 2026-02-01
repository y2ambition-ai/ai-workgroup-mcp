# CCBridge v12 迁移指南

## 概述

v12 版本从共享数据库架构迁移到每个 Agent 独立数据库架构，彻底消除并发锁竞争问题。

## 变更摘要

| 对比项 | v11 (旧版) | v12 (新版) |
|--------|-----------|-----------|
| 数据库文件 | 2 个共享文件 | 每 Agent 1 个独立文件 |
| 并发控制 | 多 Agent 抢同一个 DB 锁 | 无竞争，每个 Agent 只访问自己的 DB |
| Leader 选举 | 基于 lease 的复杂选举 | ID 最小的在线 Agent 即为 Leader |
| 消息传递 | 写共享 messages 表 | 写自己的 outbox，Leader 搬运到目标 inbox |

## AI 无感迁移

**MCP 工具接口完全不变：**
- `get_status()` - 查询在线状态
- `send(to, content)` - 发送消息
- `recv(wait_seconds)` - 接收消息

## 手动迁移步骤

1. **备份旧数据（可选）**
2. **切换到 v12** - 使用 `bridge_v12_main.py`
3. **验证** - 启动多个 Agent 测试
4. **清理旧数据** - 确认正常后删除旧数据库

## 新数据库位置

```
C:/mcp_msg_pool/
├── bridge_agent_001.db
├── bridge_agent_788.db
└── ...
```

## 故障排查

### Agent 无法互相发现
确保所有 Agent 使用相同的 `C:/mcp_msg_pool` 路径

### 消息发送后收不到
检查 Leader 是否正常运行（ID 最小的 Agent）

### ID 用完
清理过期 Agent 的数据库文件
