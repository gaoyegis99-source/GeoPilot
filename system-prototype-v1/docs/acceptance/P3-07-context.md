# P3-07 任务上下文前置验收

2026-11-11 主控签收上下文接口。依据 V2.0 §7.2 和 [P3-07 的既有前置](../../plans/04_TASKS/P3/P3-07.md)。新增 `src/geopilot/domain/task.py`、`src/geopilot/application/task_context.py` 和 `tests/application/test_task_context.py`。

构建器通过 P1 已验收的重开服务读取用户明确选定的资产版本，按方法输入角色记录 `READY`、`CONDITIONAL`、`BLOCKED` 或 `UNKNOWN` 与具体原因。给定工具注册表时，对直接绑定输入的实际元数据应用该工具的测量策略。后续步骤生成的成果由各自的执行前检查复核。接口返回可序列化的任务上下文，方案草稿及保存由 P3-07 至 P3-09 接入。

- 先行运行 `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest tests/application/test_task_context.py -q` 退出 2，缺实现。
- 最终运行 `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest tests/application/test_task_context.py -q --junitxml=artifacts/test-results/p3-task-context-junit.xml`，退出 0，`7 passed in 1.00s`。[JUnit](../../artifacts/test-results/p3-task-context-junit.xml)
- 真实 SQLite 与 GeoPackage 测试覆盖所选版本、缺角色、类型不符、未知版本、源内容变化，以及 3857、未知 CRS、6933 图斑的实际适用判断。没有测量要求的属性处理可使用可读但缺少 CRS 的矢量。

接口已可用。P3-07 草稿生成仍需 P3-02 和 P3-06 的验收。
