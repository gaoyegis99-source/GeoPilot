# P13-05 暂存资源复验

## 矢量子进程的导入开销

M 档三个矢量负载的候选 RSS 观测中位数较高。原候选 `e874a0f` 的矢量写入器导入暂存模块，顶层依赖又经步骤适配器加载 Rasterio。修正版 `a2d2e6874fd7d5d2a239de9d95a927f8505e2ebc` 将父进程所需的依赖延迟至相关函数调用，只改动 `output_staging.py`。暂存、发布与恢复逻辑保持原实现。

使用正式 M 的原生镜像，原版、原候选和修正版对五个入口分别启动三个独立 Python 进程，共 45 次，均退出 0。修正版在原候选只读源码上覆盖该单个文件，覆盖文件与冻结提交逐字节一致。

| 导入或工具入口 | 原版 RSS 中位数（MiB） | 原候选（MiB） | 修正版（MiB） | Rasterio 加载次数（原版 / 原候选 / 修正版） |
| --- | ---: | ---: | ---: | ---: |
| 矢量写入模块 | 119.34 | 138.89 | 119.93 | 0 / 3 / 0 |
| 空间关系工具 | 120.32 | 139.98 | 120.70 | 0 / 3 / 0 |
| 矢量裁剪工具 | 120.15 | 140.47 | 121.48 | 0 / 3 / 0 |
| 面归属统计工具 | 121.51 | 140.87 | 121.37 | 0 / 3 / 0 |
| 最短路径工具 | 141.38 | 142.02 | 141.73 | 3 / 3 / 3 |

修正版前三个矢量工具及写入模块的启动 RSS 回落约 19.0–19.5 MiB。最短路径工具仍通过原有依赖加载 Rasterio，启动 RSS 接近。修正版暂存及真实写入专项 20 项通过。正式方法的资源峰值需要用修正版重新采样。完整原始观测、命令和补丁位于 `artifacts/test-results/p13-05-staging-import-proof.jW2GNE/`。 [导入探针摘要](p13-05-staging-import-probe.jsonl) · [专项 JUnit](p13-05-staging-import-junit.xml)

## 遥感成果发布前的取消和超时

原版 `977ab41` 与修正版 `a2d2e68` 使用同一份四像元输入执行生产 `calculate_change` 工具。同步点验证差值 TIFF 和分类 TIFF 都可读，类别为 1、2、3 和 NoData。随后分别触发真实取消和 8 秒超时。两侧步骤均退出 -15，持久终态分别为 `CANCELLED/NOT_CHECKED` 和 `FAILED/NOT_CHECKED`，错误为 `PROCESS_CANCELLED` 和 `PROCESS_TIMEOUT`。

两种失败下，原版分别残留两个 TIFF，共 3,671 字节。修正版均清除两份文件和暂存目录。四次运行均保留输入指纹和已有文件，无成果发布，进程已回收且租约释放。首次夹具以普通包的方式读取命名空间包路径，提前退出 1。修正路径核验后，四次真实运行及配对核对退出 0。源码固定信息、实际日志、SQLite、文件观测及脚本位于 `artifacts/test-results/p13-05-remote-stage-control.Gguami/`。 [实际配对结果](p13-05-remote-stage-control.json)

## 数据适配和 GIS 回归

从修正版固定源副本执行 `python -B -m pytest -q -p no:cacheprovider tests/data_adapters tests/gis`，Python 3.13.13、Psycopg 3.3.4 和原 P12 固定 PostGIS 镜像。首次单 CPU 低优先级运行通过 360 项，PostGIS 并发刷新在 SQLite `BEGIN IMMEDIATE` 遇到锁，退出 1。原版与修正版的 PostGIS 刷新和 SQLite 连接实现逐字节相同。两侧单独复跑该用例均通过。恢复正常 CPU 调度后，完整套件同样的 361 个用例全部通过，0 错误、0 跳过，退出 0。命令、源摘要及原始输出位于 `artifacts/test-results/p13-05-domain-tests.xmIqZI/`，入库的首次日志和 JUnit 去除行尾空格。 [首次日志](p13-05-domain-tests-initial.log) · [首次 JUnit](p13-05-domain-tests-initial-junit.xml) · [两侧专项复跑](p13-05-domain-postgis-recheck.log) · [完整复跑](p13-05-domain-tests.log) · [JUnit](p13-05-domain-tests-junit.xml)

修正版 M 复测已从 `artifacts/test-results/p13-05-vector-m-revised-plan.qp9bS7/run-m-revised.py` 启动。四个原版分片已经用冻结比较器独立校验并保留，修正版另生成 56 个运行后按输入及环境回执逐组对照。输出根为 `/mnt/datab/geopilot-p13-05-vector-m-revised.a2d2e68`。L 矢量和栅格启动器也已固定同一修正版，分别位于 `artifacts/test-results/p13-05-vector-l-plan.a2d2e68.C905zE/` 与 `artifacts/test-results/p13-05-raster-plan-a2d2e68.TONYtP/`。三者共用正式测量锁。

首个修正版点相交分片 14 次全部通过。与原版对照的冷态墙钟中位数为 8.08 → 2.89 秒，暖态为 8.11 → 3.08 秒。原版与修正版的观测 RSS 中位数约为 142 MiB，冷热组均有 partial，仍不计算 RSS 差值。独立重跑冻结比较器，输出与启动器保存的结果逐字节相同。原始证据在上述输出根，复跑结果在启动器目录的 `first-pair-recheck.json`。

后续三个修正版分片均已完成，四组共 56 次新运行通过。 [修正版 M 完整结果](p13-05-vector-m-revised.md)

## 暂存与取消的组合回归

主控在 `0371589578b6c2dbf985f61298552a170a562a40` 的独立工作树叠加两个性能候选提交，得到预演 `ca55a233a84f3c2171ea4a6af8d08ec6b52e6264`。Python 3.13.13、SQLite 3.51.2 上运行 `tests/runners/test_output_staging.py` 和 `tests/scheduler/test_cancellation.py`，23 项通过，0 失败、0 跳过，退出 0。覆盖真实子进程取消和超时、租约及重启恢复、SQLite 锁竞争下取消观察与发布边界。 [命令与日志](p13-05-cancel-staging-preview.log) · [JUnit](p13-05-cancel-staging-preview-junit.xml)

D 核对了两个改动的调用链。标准服务路径持有同一工作进程租约直到暂存清理完成，取消观察和恢复获取租约均采用非阻塞策略。

主控从 `d03d7b3a99521b6770e5c5762730bf045d485211` 重放四项性能候选，生成隔离预演 `96665d486bf32ae2ad711c50043f355e8f64bc74`。状态诊断文件与集成提交逐字节相同。裁回、空间关系、栅格面积、受管写入、暂存、取消、状态查询和运行来源共 121 项通过，0 失败、0 跳过，退出 0。测试将未处理的线程异常作为失败。归档包含命令、JUnit、候选源码 Git bundle 及后端测试收集。 [组合预演及原始证据](p13-05-current-integration-evidence.tar.gz)

主控从 `e04b29b4daa3e7ec4b65287230cb8d3e6166fa8f` 在独立工作树重放六个候选提交，得到 `a131a5139093047b337302c893d12e709ed04d3b`。相对组合 `c001411`，生产源码差异仅为有界栅格布局索引，实际文件与隔离候选 `2d14c81` 逐字节一致。Linux 5.15、Python 3.13.13、SQLite 3.51.2 上，面积、空间关系、裁回、受管写入、暂存、取消和状态查询专项 112 项通过，栅格基准分类、请求与计量专项 10 项通过，两次退出 0。组合继续作为隔离预演，正式性能候选仍待对应配对。 [准确源码 bundle、命令和 JUnit](p13-05-combined-preview-evidence.tar.gz) · [布局候选](p13-05-raster-m-l.md#有界栅格布局索引预研)

## 分区重投影与终态回执

A 在注册的 `python-land-use-assign` 工具执行真实 CRS 重投影时触发取消。冻结候选留下 98,304 字节的临时 GeoPackage。独立提交 `b126785a5d51ecff2a27bbb2c6cbefc4171e55ad` 使用现有受管目录定位器。修补后子进程仍以 -15 退出，临时文件及目录被回收，分配成果未发布。正常执行及独立 CLI 均得到预期的 100 m² 分配。主机小型回归 10 项通过，固定原生镜像专项 3 项通过，均退出 0。补丁尚在隔离分支。 [分区重投影入口](../../../src/geopilot/gis/tools/land_use_transition.py#L52)

D 在生产 worker 正常退出后注入一次 `EBUSY` 删除错误，运行仍 `SUCCEEDED/PASSED`，留下受管空目录和所有权回执。恢复删除能力后，启动恢复扫描跳过该终态运行。独立复验在服务停止、取得调度与工作进程租约、读取该运行的持久步骤请求后调用现有恢复函数，清除了已核对设备和 inode 的目录及回执。正式 GeoPackage、成果清单、数据库终态和同名前缀的未登记目录均保持原字节或记录。两次探针各 1 项通过，退出 0。 [红绿测试、补丁及终态恢复探针](p13-05-staging-edge-evidence.tar.gz)

### 暂存回执写入与清理失败

D 和主控在冻结修正版 `a2d2e68` 与含相邻批次投影复用的组合预演 `c001411` 中，分别运行同一组 11 项故障注入。两版的暂存、进程管理、租约和恢复代码一致。控制组实际启动子进程并正常清理。回执打开或写入时注入 `ENOSPC`，以及完整写入后同步时注入 `EIO`，三种情况下均未启动子进程。文件系统允许清理时，目录和残存回执均被清除。 [脚本、命令、四次原始日志和 JUnit](p13-05-staging-receipt-faults-evidence.tar.gz)

若同一时刻清理也因 `EBUSY` 失败，缺失或不完整的回执不能支持按目录身份自动回收。完整可读的回执在清理失败时保留，解除故障后可在同一租约下定向回收。创建目录后获取身份失败，或者进程在回执写入完成前强制退出，探针分别观察到一或两个未登记的空目录，未启动 GIS 子进程。运行中的实例重启后仍记为 `FAILED/RUN_INTERRUPTED`。证据保留了同名前缀的对照目录。

用 4,096 条历史终态记录核对启动路径，SQLite 查询扫描 `run` 并为排序使用临时 B-tree，暂存恢复访问终态工作目录的次数为 0。具备有效回执时可按下文定向回收。缺少可验证的回执时保留现场供人工定位。两版四次各 11 项通过，退出均为 0。环境为 Linux 5.15、Python 3.13.13、SQLite 3.51.2。

```bash
p13_receipts=$(mktemp -d)
tar -xzf docs/round2/performance/p13-05-staging-receipt-faults-evidence.tar.gz -C "$p13_receipts"
(cd "$p13_receipts" && sha256sum -c SHA256SUMS)
bash "$p13_receipts/replay.sh" "$PWD/.venv/bin/python"
```

### 按终态步骤回执定向恢复

停止对应项目服务后，从包含暂存修复的原型目录执行。将 `python_bin`、`db`、`workspace`、`run_id` 和 `step_key` 设为实际解释器、项目库、工作区、运行和步骤。命令经第三个隔离样本原样复跑，退出 0。结果清单与数据库摘要均未变化。完整命令输出随上面的原始证据归档提供。

```bash
PYTHONPATH=src PYTHONDONTWRITEBYTECODE=1 "$python_bin" - "$db" "$workspace" "$run_id" "$step_key" <<'PY'
from pathlib import Path
import sys

from geopilot.execution.output_staging import recover_output_staging
from geopilot.execution.step_support import output_staging_receipt_path
from geopilot.execution.worker_lease import exclusive_local_lock, worker_lease
from geopilot.persistence.plan_repository import row_for_plan
from geopilot.persistence.run_repository import get_run, get_run_step
from geopilot.persistence.schema import open_database
from geopilot.persistence.workspace import RunWorkspace

database_path, workspace_root, run_id, step_key = sys.argv[1:]
database = Path(database_path).resolve(strict=True)
workspace = Path(workspace_root).resolve(strict=True)
if not database.is_file() or not workspace.is_dir():
    raise SystemExit("Expected an existing project database and workspace")
terminal = {"SUCCEEDED", "FAILED", "CANCELLED"}
with exclusive_local_lock(database.with_name(f"{database.name}.scheduler.lock")):
    with open_database(database, read_only=True) as connection:
        run = get_run(connection, run_id)
        step = get_run_step(connection, run_id, step_key)
        if (run is None or step is None or run.execution_status not in terminal
                or run.check_status == "CHECKING" or step.status not in terminal
                or step.request is None or not step.request.output_targets):
            raise SystemExit("Expected an exact terminal run and step with persisted output targets")
        plan = row_for_plan(connection, run.plan_id, run.plan_version)
        if plan is None:
            raise SystemExit("The exact confirmed plan is unavailable")
        workdir = RunWorkspace(workspace, plan["task_id"], run.id).path
        if workdir != Path(run.workdir) or workdir.resolve(strict=True) != workdir:
            raise SystemExit("Recorded run directory does not match the configured workspace")
        request = step.request.model_dump(mode="json")
        if (request["run_id"], request["step_key"]) != (run.id, step.step_key):
            raise SystemExit("Persisted request does not identify the selected run and step")
    with worker_lease(workdir):
        receipt = output_staging_receipt_path(workdir, request)
        if receipt.is_symlink() or not receipt.is_file():
            raise SystemExit("No eligible regular ownership receipt. Leave the files in place.")
        recover_output_staging(workdir, request)
        if receipt.exists() or receipt.is_symlink():
            raise SystemExit("Cleanup incomplete. Preserve the receipt and inspect the filesystem error.")
        print(f"Recovered owned staging for {run.id}/{step.step_key}")
PY
```

## 四项性能修复集成

主控在审计分支依次重放 `e874a0f`、`a2d2e68`、`b126785` 和 `fff225f`。得到生产与测试提交 `217320baec299e4a66a34042fb353bad0d703749`。A 校验预演补丁及逐路径内容，主控复核与四项预演 `938dfd9` 的生产、配置、脚本和测试逐字节一致。后续两项投影布局候选继续等待各自的整幅或遥感证据。DEM M 重试仍使用原只读冻结源码。 [组合预演](p13-05-combined-preview-evidence.tar.gz) · [T14 大规模正式配对](p13-05-vector-l.md#t14-局部裁回候选完整配对)

主控以 Linux 5.15、Python 3.13.13、Shapely 2.1.2、Rasterio 1.4.3 和 SQLite 3.51.2，执行 `tests/data_adapters`、`tests/gis`、`tests/runners`、取消和状态查询专项。共 478 通过、22 跳过、0 失败，退出 0。13 项跳过需要 psycopg，9 项需要 QGIS 原生运行时。命令、JUnit、原始日志、修改清单及跳过原因在归档中。可按 `217320b`、`6acd934`、`7848cd6`、`711e417` 的逆序逐项撤销。P13-05 等待剩余栅格证据，正式发布仍需最终提交的回归。 [同源码集成回归](p13-05-integrated-resource-regression-evidence.tar.gz)

## 宿主跳过节点的原生复跑

A 和 B 从宿主回归的 JUnit 提取 22 个跳过节点，在精确源提交 `cf53e827a93ff4f56cca8541827b26ff858ed736` 的独立副本复跑。主控核验两次源提交之间相关生产、配置和测试文件逐字节一致。按 JUnit 类名和用例名逐项对照，两组分别覆盖原来的 13 项与 9 项跳过，均退出 0。宿主和两次复跑合计覆盖原 500 个节点。 [节点、实际命令、环境、JUnit 与复验程序](p13-05-integrated-environment-replays-evidence.tar.gz)

- A 使用 Python 3.13.13 和 Psycopg 3.3.4，在 CPU 29 运行 13 个真实 PostGIS 节点。测试使用四个自有实例，镜像摘要与 P10 已验收版本一致。13 项通过，耗时 17.50 秒，四个实例均已退出。
- B 使用固定原生镜像中的 Python 3.12.3 和 QGIS 3.44.15 CLI，在 CPU 30 执行 9 个真实 QGIS 节点。9 项通过，耗时 31.47 秒。首次预检因可选 `osgeo` 导入退出，pytest 尚未启动。正式复跑用 CLI 获取 GDAL 3.8.4 与 PROJ 9.4.0 版本。两次复跑的可写输出均位于隔离 tmpfs，DEM L 容量探针使用 CPU 31。

## P13-05 主控验收

状态 ACCEPTED。P13-03 和 P13-04 已验收。13 个严格配对在同一输入与环境下复算 350 个唯一运行。已完成的矢量和栅格候选样本通过独立正确性核对。DEM L 在延长预算下三份整幅输出完全一致，跨批投影复用保留逐格收敛与原精度。正式方法 60 秒预算下的 DEM L 全部超时，遥感及 DEM M 的状态观察失败仍留在各自的正式统计中。CPU 和 I/O 为观测下界，26 个冷热组的成功 RSS 差值为空。 [正式资源统计](p13-05-resource-coverage.md#纳入-dem-m-重试的全部已封存配对) · [DEM L 整幅证据](p13-05-raster-m-l.md#跨批投影复用整幅结果)

最终生产源码 `f16f72d98383205b053225957e439d856ba90da1` 在原生 QGIS 和真实 PostGIS 环境执行 `tests/data_adapters tests/gis`。367 项通过、0 失败、0 跳过，退出 0，111.26 秒。宿主 Python 3.13.13、Psycopg 3.3.4，QGIS CLI 3.44.15。命令、环境、JUnit 和实际变更清单已保存。 [最终集成回归](p13-05-area-integration-evidence.tar.gz)

主控已审查任务初始范围外的执行层暂存和调度终态清理改动，用于覆盖相关 GIS 输出在成功、异常、取消和重启下的生命周期。完整回执的暂存路径可定向清理，缺少回执的空目录继续保留，终态删除遇文件系统故障时以既有恢复命令处理。对外 API 和生产步骤限时沿用原契约。撤销跨批复用可回滚 `f16f72d`，整批回滚按 `f16f72d`、`217320b`、`6acd934`、`7848cd6`、`711e417` 的逆序。固定密化边界研究留在隔离证据中。P13-06 前置通过，P13 阶段继续等待缓存及并发 ADR。 [暂存故障复验](#暂存回执写入与清理失败)
