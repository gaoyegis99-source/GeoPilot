# P3 道路七步与检查状态实跑

依据 V2.0 §7.9、§8.7、§11.3 和 §12.3.4，使用 QGIS 3.44.15 与已登记的 Python 子进程，以 SQLite 中的 `plan-001@1` 确认快照运行 `road-buffer-statistics@2`。两次运行的目录和数据库记录独立，七个步骤均退出 0，四条结果规则通过。确认前后均核对方案版本，确认时运行表为空。样例采用 EPSG:32650、500 米局部投影缓冲和单点计数。

| 固定用例 | 实际断言 | 结果 |
|---|---|---|
| T07 | 直接测得 `road-edge` 距分析区 300 m，实际取数纳入该道路，裁回保留受其影响的 `building-edge-road`。 | PASS |
| T08 | 直接测得 `road-far` 距分析区 650 m，实际取数排除该道路，裁回不含 `building-far-road`。 | PASS |

两次运行的取数集合均为 `road-edge` 和 `road-main`，裁回前七个、裁回后六个目标的 ID 逐项匹配 P0 参考。裁回点与源坐标精确一致，西区 3、东区 2、未归属 1。复算的 CSV 字节一致，缓冲面积和边界在既定容差内。输入文件组指纹保持一致，实际成果清单的图层、类型、相对路径及指纹均已核对。[第一次道路规则报告](../../artifacts/smoke/p3-stage/run.9yMUWWEj/pytest/test_road_workflow_passes_fixe0/workspace/tasks/task-001/runs/road-first/checks/report.json) · [复算规则报告](../../artifacts/smoke/p3-stage/run.9yMUWWEj/pytest/test_road_workflow_passes_fixe0/workspace/tasks/task-001/runs/road-repeat/checks/report.json) · [成果清单](../../artifacts/smoke/p3-stage/run.9yMUWWEj/pytest/test_road_workflow_passes_fixe0/workspace/tasks/task-001/runs/road-first/outputs/manifest.json)

同一入口完成属性筛选。真实 QGIS 成功后，返回正常不通过判定时记录 `SUCCEEDED / FAILED`，检查器抛异常时记录 `SUCCEEDED / NOT_CHECKED`，产出仍可重开。中断报告保存已完成规则、待查规则和原因，SQLite 同时保留中断报告。报告发布失败的专项回归覆盖 SQLite 回退。空规则在确认时作为配置错误返回。[检查中断报告](../../artifacts/smoke/p3-stage/run.TzRjLPzd/pytest/test_checker_interruption_pres0/workspace/tasks/task-001/runs/attribute-check-interrupted/checks/report.json)

实际执行采用 `GEOPILOT_QGIS_PROCESS="$PWD/.environments/qgis-deb/qgis_process" PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest tests/application/test_plan_run.py`，分开运行道路与属性/中断用例并用独立 `--basetemp` 保存证据。道路选择运行 1 项通过，属性与中断运行 2 项通过，均无失败或跳过。[道路 JUnit](../../artifacts/test-results/p3-road-workflow-junit.xml) · [检查集成 JUnit](../../artifacts/test-results/p3-check-integration-junit.xml)

最终累计回归重新执行以上三个整链用例，并包含 8 项检查中断专项。报告写入失败与发布中断的回退均在累计报告中可追溯。[累计 JUnit](../../artifacts/test-results/p3-stage-junit.xml)

T13、T14 仍按 P6 验收。T16 的后台调度、状态查询及完整用例仍按 P4 验收。
