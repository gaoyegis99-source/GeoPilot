# 第二轮 Agent 文件所有权

| Agent | 职责 | 首选范围 |
|---|---|---|
| A | 平台/数据可靠性 | 数据诊断、资源采样、资产一致性 |
| B | GIS 执行 | runner、GIS benchmark、结果对照 |
| C | 方法生态 | schema、CLI、配置校验、版本兼容 |
| D | 调度可靠性 | scheduler、取消、恢复、队列压力 |
| E | 前端/UX | 工作台性能、错误解释、用户任务 |
| F | QA/性能 | tests、benchmarks、证据扫描、CI |
| G | Agent/LLM | provider、澄清、反馈解释、计划修订 |
| 主控 | 架构/集成 | 公共接口、阶段门禁、最终验收 |

公共 Pydantic 模型、SQLite schema、API contract、方法 schema 和状态枚举均为共享文件。修改前提交 RFC，由主控指定唯一修改者。
