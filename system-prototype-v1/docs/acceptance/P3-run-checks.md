# P3 方法运行结果检查

Agent-B 建议 `REVIEW`。检查规则对应 V2.0 §7.9、§10.1.5 和附录 A.1。新增 `src/geopilot/application/run_checks.py`、`tests/application/test_run_checks.py`、本记录和测试证据。

## 接口与判定

`RunCheckContext` 保存确认方案、运行目录、暂存资产引用、步骤结果和成果引用。`RUN_CHECKS` 按配置中的规则名查找函数。每条规则返回 `RunCheckResult(passed, message, observed)`，保留可序列化的观察值。所有文件读取使用运行目录相对引用，拒绝运行目录内部的符号链接。调用方保存执行和检查状态。

| 规则 | 实际依据 |
|---|---|
| `output_exists` | 按方案成果角色重开实际文件与指定图层，报告缺失、类型错误或读取失败的角色。 |
| `measurement_policy_satisfied` | 对比两步缓冲的确认距离、持久化请求、QGIS 报告的 `DISTANCE`、测量审计，以及重开的输入与输出 CRS。 |
| `outside_roads_within_influence_included` | 用 Shapely 独立求暂存道路与实际外扩范围的相交集合，比较候选道路 ID，列出缺失与多出的 ID。 |
| `output_clipped_to_analysis_extent` | 重开裁回成果和原分析范围，检查每个目标点是否被范围覆盖。边界点通过。 |
| `detail_count_matches_summary` | 重开点成果与 CSV，对比去重目标 ID、每行计数、已归属与未归属总数。重复、缺失和多出的 CSV ID 分别记录。 |

## 实际验证

```bash
mkdir -p artifacts/smoke/run-checks
p3_checks_evidence=$(mktemp -d artifacts/smoke/run-checks/run.XXXXXX)
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 GEOPILOT_QGIS_PROCESS="$PWD/.environments/qgis-deb/qgis_process" \
  python -m pytest tests/application/test_run_checks.py -q --basetemp="$p3_checks_evidence/pytest" \
  --junitxml=artifacts/test-results/p3-run-checks-junit.xml
```

| 验证 | 输出与结果 |
|---|---|
| 实现前 `python -m pytest tests/application/test_run_checks.py -q` | `ModuleNotFoundError: No module named 'geopilot.application.run_checks'`，退出 `2`。 |
| 实现后专项命令 | `13 passed in 10.75s`，退出 `0`，无跳过。[JUnit](../../artifacts/test-results/p3-run-checks-junit.xml) |
| `python -m pytest tests/application/test_run_checks.py tests/application/test_method_preconditions.py tests/application/test_task_context.py -q` | `22 passed in 20.12s`，退出 `0`。 |
| 两步真实 QGIS `native:buffer` | QGIS `3.44.15-Solothurn`，两步实际距离 500 m、CRS EPSG:32650。篡改请求距离、实际报告距离、审计单位或输入 CRS 均产生检查失败，原步骤状态保留 `SUCCEEDED`。[外扩日志](../../artifacts/smoke/run-checks/run.IOctX7/pytest/run-check-buffers0/steps/expand_analysis_extent/process.log) · [道路缓冲日志](../../artifacts/smoke/run-checks/run.IOctX7/pytest/run-check-buffers0/steps/create_buffer/process.log) |
| P0 实际 GeoPackage 与 CSV | 漏选 `road-edge`、误选 `road-far`、裁回后含区外点、CSV 重复 ID、计数错误及等量错误 ID 均被检出。保留的重复点行按原始 ID 去重核对。 |

## 适用范围

道路规则按 P3 的 v2 点目标、局部投影和分区计数配置验证。计数规则核对明细 ID 与汇总，分区归属及 T07/T08 的端到端参考由阶段运行核对。[P0 道路参考](../../tests/fixtures/vector/expected.json)

整目录 `tests/application` 在本次并行开发期间因 `test_plan_run.py` 引用尚未落盘的 `geopilot.application.plan_runner` 而退出 `2`。上表记录已完成模块的联合结果，阶段运行任务复测整目录。
