# P11-06 运行生命周期与故障演练

2026-11-12，Agent-A 执行，主控签收。按 V2.0 §7.6、§7.9、§8.7 和 §12.3.5，复跑已验收的调度、步骤执行、检查报告与重查入口。使用独立 SQLite 和运行目录，实际启动 Python GIS 与 QGIS，复用现有 HTTP 路由和 SQLite v4。[设计](../../../docs/source/GeoPilot_产品需求规格与系统详细设计报告_V2.0.md) · [等价测试入口](../../plans/01_ARCHITECTURE/05_REPOSITORY_PATH_MAP.md) · [P4 阶段验收](P4.md)

## 实际执行

Python 3.13.13、SQLite 3.51.2、QGIS 3.44.15。下列命令在原型目录退出 `0`，输出 `62 passed, 1 warning in 127.94s`。JUnit 为 62 项、零失败、零错误、零跳过。提醒来自 Starlette 测试客户端引用 AnyIO 已弃用的别名。[JUnit](../../artifacts/test-results/p11-06-junit.xml) · [原始输出](../../artifacts/smoke/p11-06/run.1HiQwqlr/pytest.log)

```bash
set -o pipefail
p11_lifecycle_dir="$PWD/artifacts/smoke/p11-06/run.1HiQwqlr"
GEOPILOT_QGIS_PROCESS="$PWD/.environments/qgis-deb/qgis_process" PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 PYTHONDONTWRITEBYTECODE=1 \
  python -m pytest tests/scheduler tests/runners/test_process_timeout.py tests/checks/test_recheck.py \
    tests/checks/test_recheck_service.py tests/api/test_check_reports.py tests/api/test_run_status.py \
    tests/runs/test_retry.py tests/runs/test_run_api.py -q -p no:cacheprovider \
    --basetemp "$p11_lifecycle_dir/pytest" --junitxml=artifacts/test-results/p11-06-junit.xml \
  2>&1 | tee "$p11_lifecycle_dir/pytest.log"
```

## 实测状态与证据

| 场景 | 执行状态 / 检查状态 | 实测记录 |
|---|---|---|
| 启动失败、非零退出、超时、无效清单、缺失或损坏成果 | `FAILED / NOT_CHECKED` | 三步链均停在第二步 `consume`。启动失败无退出码，非零退出实际为 `7`，链内超时为 `-15`。第一步成功，第三步保持 `NOT_STARTED`。可重开的部分 GeoPackage 和日志仅作为排错证据。 |
| 排队、运行中、两步之间取消 | `CANCELLED / NOT_CHECKED` | 排队无已执行步骤。活动取消先返回 HTTP 202 并持久化意图，实际进程以 `-9` 停止，终态错误 `PROCESS_CANCELLED`。步间取消保留第一步退出 `0`，后续步骤待执行。 |
| 受管进程组超时 | 步骤 `FAILED / PROCESS_TIMEOUT` | 忽略 SIGTERM 的组长退出 `-9`。另一组长退出 `-15`，其关闭日志的后代也停止。两份日志各保留 stdout、stderr 和真实退出码，诊断 CSV 的面积均为 `100` 平方米。 |
| 计算中服务重启 | `FAILED / NOT_CHECKED` | 旧进程持锁时服务拒绝恢复和派发。旧锁释放后，同一运行和确认版本记录 `RUN_INTERRUPTED`，活动 GIS 步骤失败，后续步骤保持待执行。 |
| 成果登记提交前服务重启 | `FAILED / NOT_CHECKED` | 三个实际计算步骤均退出 `0`，未提交的成果清单被清理，SQLite 正式成果为零。已生成文件可作排错证据。 |
| 首次检查报告提交前服务重启 | `SUCCEEDED / NOT_CHECKED` | 保留三个成功步骤和可读正式成果。原预留报告编号发布为 `CHECK_INTERRUPTED`，待查项为 `output_exists`，未提交的兼容报告文件被清理。 |
| 真实 QGIS 成果阻断、警告、检查异常 | `SUCCEEDED / FAILED`、`SUCCEEDED / WARNING`、`SUCCEEDED / NOT_CHECKED` | `t16-plan@2` 的三个实际 QGIS 都退出 `0`，各自 GeoPackage 仅含 `building-west`。HTTP 状态、当前报告与 SQLite 一致。 |
| 检查异常或用户取消后重查 | 初次 `SUCCEEDED / NOT_CHECKED`，重查 `SUCCEEDED / PASSED` | `recheck-plan@2` 的两个实际计算步骤均退出 `0`。重查保留同一运行编号、方案版本、规则版本、已登记成果和旧报告，发布新当前报告。原请求、日志、结果和 GeoPackage 的内容、inode 和修改时间通过前后比较。 |
| 计算失败后重试 | 旧运行 `FAILED / NOT_CHECKED`，新运行 `SUCCEEDED / PASSED` | 分别从原确认版本 1 与新确认版本 2 创建独立运行，从位置 0 执行，重开实际输出包含三条道路。旧运行及证据保留。 |

## 独立读取核对

按只读模式复核 20 个实际 SQLite、22 个运行。`integrity_check` 均返回 `ok`，`foreign_key_check` 无记录。运行快照中的 ID 和完整方案 JSON 与数据库同一确认版本一致。检查记录的 `run_id`、报告 ID、状态和版本与运行指针一致，磁盘兼容报告按对应历史编号逐值比较。正式 GeoPackage 重新打开，核对来源方案版本、要素及内容指纹。对三个超时组成员和一个活动取消进程直接读取 `/proc`，四个 PID 均已不存在，实际运行目录的进程锁可重新取得。[结构化复核](../../artifacts/smoke/p11-06/run.1HiQwqlr/verification.json) · [本次运行目录](../../artifacts/smoke/p11-06/run.1HiQwqlr/pytest/)

真实故障触发由现有测试夹具控制，验证平台为本机 Linux。新增交付为本报告、本轮 JUnit、原始 pytest 日志、测试留下的真实 SQLite 与运行目录，以及上述只读复核记录。
