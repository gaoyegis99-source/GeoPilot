# 文件所有权与共享文件规则

| 文件/目录类别 | 主责任人 | 可协作角色 | 规则 |
|---|---|---|---|
| 公共 Domain models / API schema | 主控 | 对应 Agent 提案 | 一次只允许一名修改者 |
| SQLite schema/migrations | Agent-A | 主控审查 | 迁移必须向前兼容或说明重建策略 |
| Data adapters / repositories | Agent-A | F 测试 | 不直接改 scheduler |
| GIS algorithms / runners | Agent-B | F 测试 | 不直接改 API/domain 公共契约 |
| Methods / plans / compiler | Agent-C | B/F | 固定步骤不能由模型重排 |
| Scheduler / run lifecycle | Agent-D | A/F | 只由服务层更新 SQLite |
| Frontend | Agent-E | D/F | 使用已冻结 API schema |
| Tests / fixtures / reports | Agent-F | 全体 | 修改 expected 需说明参考依据 |
| LLM adapter | Agent-G | C/F | 不可直接运行 GIS 工具或确认方案 |

Agent 修改文件前检查 git status 和现有未提交改动。不得覆盖他人未提交修改。公共文件冲突先停止，向主控提交 BLOCKER。
