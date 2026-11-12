# P13-05 资源优化的正确性复验准备

P13-05 等待矢量基准验收。本轮增加面积回归用例和小样本对照证据，生产算法继续使用原实现。测试基于 `5ef9b042528c1f6b55461b9ad5dffa8e24317fe2` 加新增测试。Python 3.13.13、PROJ 9.5.1、GDAL 3.9.3、GEOS 3.13.1。

## 等积面积

- A 的粗剪切分类栅格在生产实现中细分至 256 段。固定八段的最大单类相对差为 `3.70e-7`。同一类别的 257 个像元样本产生两个批次，分别使用 64 段和 32 段。 [原始探针](p13-05-preflight-area-probe.log)
- 非零仿射行列式的 4 × 2 栅格仍出现零面积像元。生产逐格校验拒绝该输入，按类合并的边界积分却返回了正面积。反经线以及 EPSG:8858 的 90°E、EPSG:8859 的 30°W 切缝也触发现有投影范围校验。最初切缝试探的两个 EPSG 经纬位置互换，使用实际切缝位置的结果见复测。 [切缝与退化复测](p13-05-preflight-area-cuts.log)
- 主控在每个原批次内复用同一格点的投影值，仍按原顺序逐格计算和细分。15 个合成输入的报告或错误完全一致，三个真实分类栅格窗口的逐格面积数组和各批细分数完全一致。两个 DEM 窗口的变换输入各从 56,840 点降至 16,549 点，遥感窗口从 30,464 点降至 10,138 点。此记录采用同一数值路径比较点数，完整栅格耗时和内存需在生产候选上复跑。 [坐标复用探针](p13-05-preflight-area-reuse.log)
- A 在 `78b8174cb25a74ec74dc83db3ae3a5ccd09e15b4` 的独立副本实现批内投影坐标复用。18 项面积测试通过，8 个微型 GeoTIFF 的完整结果或错误与基线精确一致，输入指纹一致。密集 256 格样本的 PROJ 入点从 14,336 降至 4,097，收敛分段和逐格面积一致。隔离补丁、命令、JUnit、对照脚本和日志的校验清单保存在 `artifacts/test-results/p13-05-area-candidate.HU5Lhc/SHA256SUMS`。正式墙钟时间和峰值内存待测。
- 主控加载固定原版和 A 的候选，以原生基准保留的三个分类窗口复跑逐格面积，退出 0。全部像元面积、各批收敛分段和变换点数与先前探针一致，输入 SHA-256 与便携清单一致。 [候选窗口原始对照](p13-05-preflight-area-candidate.log)

主控再从三个已核验分类输入各提取一个 128 × 128 窗口。对基线 `3d19fe4` 和隔离面积候选各启动三个独立 Python 进程，交替先后次序，执行完整 `summarize_threshold_area`。18 次结果的对应报告逐项一致，窗口和原始输入 SHA-256 均保持。墙钟只计面积函数，峰值 RSS 来自 Linux `getrusage` 并包含 Python 进程加载开销。CPU31、nice 19、Python 3.13.13、NumPy 2.4.4、PROJ 9.5.1、GDAL 3.9.3。命令退出 0。 [原始报告与环境](p13-05-preflight-area-profile.log)

| 窗口 | 选中像元 | 原版 / 候选耗时中位数（ms） | 原版 / 候选峰值 RSS 中位数（MiB） |
|---|---:|---:|---:|
| DEM M | 16,375 | 295.9 / 224.1 | 162.30 / 162.33 |
| DEM L | 16,375 | 294.9 / 222.2 | 162.31 / 162.55 |
| 遥感 L | 8,320 | 167.1 / 130.7 | 161.70 / 163.09 |

窗口对照脚本为 `artifacts/test-results/p13-05-area-candidate.HU5Lhc/profile-windows.py`。运行时在隔离的 `3d19fe4` 源导出副本保留原 `area_stats.py` 为脚本同目录的 `baseline.py`，将候选补丁应用至副本的 `src`，从原型目录用该副本作 `PYTHONPATH`。将 `profile-windows.py` 复制至副本根目录后执行。整幅耗时、内存与方法结果仍待测。

```bash
env PYTHONPATH="$profile_tmp/src" PYTHONDONTWRITEBYTECODE=1 OPENBLAS_NUM_THREADS=1 \
  OMP_NUM_THREADS=1 GDAL_NUM_THREADS=1 PROJ_NETWORK=OFF \
  nice -n 19 taskset -c 31 .venv/bin/python "$profile_tmp/profile-windows.py" "$PWD/benchmarks/raster/area-fixtures"
```

在同一输入的 256 × 256 窗口剖析中，首版坐标复用候选在 DEM L 的 256 批中有 246 批与前一批布局相同，NumPy `unique` 执行 768 次。隔离增量只在同一瓦片内保留最近布局的 2、4、8 段索引，投影坐标仍逐批独立计算。DEM L 的 `unique` 调用降至 30 次，遥感窗口 129 批均为不同布局，调用次数保持 387。20 项面积测试通过，含粗剪切至 256 段和零面积错误路径。

对首版候选及增量各启动三个独立进程，在三份同字节窗口上复验。18 次面积报告逐项一致，原分类输入及窗口摘要保持。两个 DEM 窗口的计算中位数由 852.3、859.9 ms 降至 568.3、568.7 ms。遥感窗口由 429.3 ms 增至 438.4 ms。进程峰值 RSS 中位数变化均小于 0.5 MiB。全图耗时、RSS 和方法验证待正式测量。

增量补丁、脚本和完整原始证据位于 `artifacts/test-results/p13-05-area-layout.i0WWFE/SHA256SUMS`。在原型目录运行同目录的 `reproduce.sh` 可复跑隔离构建、剖析及专项。 [实际测量与剖析](p13-05-preflight-area-layout.log) · [JUnit](p13-05-preflight-area-layout-junit.xml)

## 暂存文件生命周期

原生 P13-04 遥感 L 的 14 次超时均留下两份暂存栅格。主控用基线 `78b8174cb25a74ec74dc83db3ae3a5ccd09e15b4` 的 `capture_process` 启动真实 Python 子进程，调用实际 `_stage_outputs`，写入模拟差值和内层分类文件，再由父进程触发超时或取消。隔离候选由父进程在输出所在文件系统分配独占目录，等子进程退出后按目录身份回收。两次探针均退出 0。 [原始结果](p13-05-preflight-staging.log)

| 场景 | 子进程退出码 | 基线暂存文件 | 候选暂存文件 | 成果发布 |
|---|---:|---:|---:|---|
| 正常退出 | 0 | 0 | 0 | 成功 |
| 超时并响应 SIGTERM | -15 | 2 | 0 | 无 |
| 超时并由 SIGKILL 终止 | -9 | 2 | 0 | 无 |
| 取消 | -15 | 2 | 0 | 无 |

每次都核验输入和既有暂存目录中的标记文件原样保留。探针以 Python 3.13.13 在独立目录执行。脚本、候选助手、子进程补丁与原始 JSONL 保存在 `artifacts/test-results/p13-05-staging-preflight.B5CwoC/SHA256SUMS`。此探针验证父进程存活且工作进程终止后的清理。

独立重启探针以 `9f3f02138d93ec52ca0a9a0409d097c10eb1cb84` 加候选助手运行。父进程在启动子进程前写入暂存目录位置、设备号和 inode。子进程写入两份暂存文件后，真实 SIGKILL 终止父进程。旧子进程继续持有租约时回收被拒。SIGTERM 终止旧子进程后，回收器在同一租约下核对声明的输出父目录和目录身份，仅清理原目录。目录身份被替换时保留替换目录。指向输入目录的回执被拒，既有输入、正式成果、历史暂存和已硬链接的诊断文件均通过字节核对。探针退出 0。 [复跑命令及原始日志](p13-05-preflight-restart.log)

重启验证使用隔离回执和真实工作进程租约。A 将暂存候选接入现有 Python 执行与启动恢复入口，在独立导出的 `3d19fe4` 源码上通过 22 项集成测试和 2 项进程组测试。真实四像元遥感工具成功产出三份成果。超时和取消后只回收本次暂存。多文件发布中途取消时，已写出的首份文件保留，失败结果不登记成果。实际父进程 SIGKILL 后，原恢复入口先拒绝仍有租约的运行，子进程退出后回收目录并保留日志及诊断。默认私有回执名与已有日志、取消文件或输出同名时，从同一请求推导可用回执名。最初测试夹具漏填工具契约字段，后续一版用宽泛的文件名匹配误判了合法同名文件。两次失败的原始日志和修正后的证据均保留。

主控将面积、空间关系、点裁剪和暂存四个补丁应用到同一隔离副本，合跑 GIS、真实 Python 执行、进程终止和启动恢复用例。81 项通过、0 失败、0 错误、0 跳过，退出 0。补丁、完整命令与分项结果保存在 `artifacts/test-results/p13-05-staging-integration.bZKBpB/SHA256SUMS`，在原型目录可用同目录的 `reproduce-combined.sh` 重跑。 [合并日志](p13-05-preflight-staging-integration.log) · [JUnit](p13-05-preflight-staging-integration-junit.xml)

隔离候选为声明输出提供通用暂存上下文。回执写完整前强杀可能留下空目录或不完整回执，此时尚未启动子进程。普通终态后的清理 I/O 失败保留回执并记日志，当前启动恢复只处理被中断的运行。完整遥感复测及新增 I/O 开销待 P13-03 验收后执行。

A 进一步将目标旁创建临时目录的矢量、DEM 和遥感写入器接入同一上下文。匹配使用完整声明输出路径，嵌套目标、未声明兄弟路径和直接调用继续使用原父目录。以实际 Python worker 在写完文件、发布硬链接前取消，旧暂存补丁分别残留 98,304 B 的 GeoPackage 和 1,313 B 的 DEM 阈值 GeoTIFF。扩展候选均回收，遥感嵌套差值在两版均回收。三种成功写入结果都通过各自独立参考。A 的写入器专项 7 项、含点裁剪与面积的联合测试 112 项通过。土地利用的临时投影区划、独立产品打包、PostGIS 导入、预览和发布账本仍走各自入口。补丁、未接入清单和前后原始输出保存在 `artifacts/test-results/p13-05-staging-writers.hOmmu7/SHA256SUMS`。

主控在 `6f90b6f` 的独立源码副本合并面积、空间关系、点裁剪、暂存与写入器五个补丁，88 项通过、0 失败、0 错误、0 跳过，退出 0。联合范围涵盖进程组终止、服务启动恢复、矢量及栅格写入。在原型目录可运行同一证据目录的 `reproduce-combined-writers.sh`。全量性能与新增 I/O 成本待测。 [合并日志](p13-05-preflight-writers.log) · [JUnit](p13-05-preflight-writers-junit.xml)

## 矢量空间关系

正式 L 原版样本的 `apply_spatial_relation` 触发生产步骤时限。主控对真实 L GeoPackage 的 64 格窗口使用原并集、准备后的并集、索引筛选后局部并集三种口径。384 点、640 线、768 面的 `intersects` 和 `within` 选中 ID 均符合独立参考。该窗口每个相交目标只有一个候选，另用相邻覆盖面反例验证跨面目标需要局部并集。现有自动化已覆盖接缝和间隙。完整规模仍须按正常工具链复跑选择、裁剪、登记和独立结果核对。 [原始窗口输出](p13-05-preflight-t13.log) · [已有接缝测试](../../../tests/gis/test_within.py)

A 的隔离候选在现有范围并集上预构建 GEOS 索引，沿用 `intersects` 和反向 `contains` 的 DE-9IM 语义。实际 L 三面小窗的点、线、面两种谓词共六次 CLI 执行均退出 0，与独立参考的 12/3、24/9、30/12 个结果相符。顺序、索引、属性、CRS 和 WKB 在新旧对照中一致。另有 6,840 次旋转、尺度、孔洞与多部件谓词对照，结果一致。单补丁 29 项通过，与点裁剪补丁隔离合并后 45 项通过。补丁、原始命令和 JUnit 保存在 `artifacts/test-results/p13-05-relation-candidate.MPdADO/SHA256SUMS`。完整 L 端到端性能待测。

L 档点相交的七次冷进程采样均在 29.496–30.517 秒完成 `apply_spatial_relation`。下一步将 65,536 个已筛选点裁回 16,384 个面覆盖范围，`clip_vector_to_extent.py` 均达到生产 60 秒时限，退出 -15。七次暖进程采样同样在裁剪超时。原始记录保存在 `/mnt/datab/geopilot-p13-03.KXUnnZ/L-remaining/evidence/samples.jsonl`。 [七次冷进程遥测和输入检查摘录](p13-05-preflight-t13-clip.log)

A 的隔离候选对确认是 XY 的 GeoPackage 点和面复用已准备的 `covers`。以真实 L 的三面、十八点窗口复跑原版与候选 CLI，先传入关系筛选后的 12 点，再直接传入 18 点。四次执行均退出 0，两套输出图层各保留相同的 12 个 ID、顺序、属性、CRS 和 WKB。首轮探针传绝对路径被现有协议拒绝，使用运行目录相对路径后完成对照。另对 1,224 个边界和邻近点比较掩膜及结果 WKB，完全一致。多点跨界和贴 Z 面边界的反例继续走现有相交运算，候选保留声明含 Z/M 的图层和空结果的原路径。16 项专项通过。源码补丁、原始日志和 JUnit 保存在 `artifacts/test-results/p13-05-point-candidate.pmbuVp/SHA256SUMS`。全量时间与峰值内存仍待测。

原版 L 线相交的两类缓存共 14 次均在空间关系步骤达到 60 秒时限，裁剪步骤未启动。A 将线面裁剪候选限定为声明与实际几何均为纯 XY、默认精度且只命中一个已溶解部件的目标。零或多个候选、GEOS 异常及 Z/M 输入继续使用完整并集。合成反例中，远侧不相交的 M 面使 ZM 目标裁剪后的顶点 Z 从局部求交的 2 变为全并集的 2.3333333333333335。

真实 L 三瓦片窗口的新旧裁剪四次退出 0，24 条线和 30 个面的两层输出在 WKB、CRS、属性、ID 和顺序上完全一致，独立参考的几何类型、长度和面积也相符。1,209 个纯 XY 合成目标的对照一致。A 的 35 项专项通过，主控在 `feab265` 的同一隔离副本应用七个补丁后 102 项通过，0 失败、0 错误、0 跳过。完整 L 性能和峰值内存仍待测。补丁、原始日志、完整 JUnit 和重放脚本保存在 `artifacts/test-results/p13-05-line-polygon-candidate.bNe9j1/SHA256SUMS`。 [局部裁剪证据](p13-05-preflight-line-polygon.log) · [联合日志](p13-05-preflight-layout-and-clip.log) · [JUnit](p13-05-preflight-layout-and-clip-junit.xml)

## 可复跑用例

新增五个回归用例，覆盖粗剪切多类等积面积、非零行列式但投影退化和三个投影切缝。与现有矢量接缝用例合跑 6 项通过、13 项未选、退出 0。原始样本未改动。 [面积回归](../../../tests/gis/raster/test_area_stats.py) · [日志](p13-05-preflight-pytest.log) · [JUnit](p13-05-preflight-junit.xml)

```bash
p13_guard_tmp=$(mktemp -d /dev/shm/geopilot-p13-05-guards.XXXXXX)
export PYTHONPATH=src PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 PYTHONDONTWRITEBYTECODE=1
export OPENBLAS_NUM_THREADS=1 GDAL_NUM_THREADS=1 OMP_NUM_THREADS=1 PROJ_NETWORK=OFF
nice -n 19 taskset -c 31 .venv/bin/python -m pytest -q -p no:cacheprovider \
  tests/gis/raster/test_area_stats.py tests/gis/test_within.py \
  -k 'coarse_sheared or collapsed_projected or projection_cut or union_across_seams' \
  --basetemp="$p13_guard_tmp/pytest" --junitxml="$p13_guard_tmp/junit.xml"
```

## 全量前后测量的对照工具

隔离候选 `run_memory_profile.py` 读取两侧的矢量或栅格基准报告及原始样本，核对记录中的输入文件组、磁盘、运行版本、采样规则及方法选项。成功与失败的墙钟、峰值 RSS 和峰值暂存各自汇总。成功统计不足三次时留空，存在 partial 观测的指标保留样本数但不输出前后差值。

在本机 Python 3.13.13 上用六批真实归档做自对照，共 74 组，均退出 0。失败样本未计入成功分位。八个独立的本地副本分别修改运行版本、输入摘要、输入前后状态、重复次数、独立验算、墙钟或方法选项，全部退出 1。自对照验证计算与拒绝路径，全规模优化数据待正式阶段。脚本及校验清单在 `artifacts/test-results/p13-05-memory-comparator.xYZIT5/`。 [命令及原始结果](p13-05-preflight-memory-comparator.log)

```bash
.venv/bin/python artifacts/test-results/p13-05-memory-comparator.xYZIT5/run_memory_profile.py \
  --kind vector --before "$baseline_evidence" --after "$candidate_evidence"
```

矢量目录使用 `summary.json`、`environment.json`、`command.log`、`inputs.json` 和 `samples.jsonl` 或其 gzip 归档。栅格和遥感使用 `--kind raster`，读取 `report.json` 和 `trials.jsonl` 或其 gzip 归档。比较仅涵盖原报告实际记录的环境身份，正式复测继续使用固定镜像及同一物理盘。工具退出 0 表示证据可对照，候选的成功次数单独记录。

F 从真实归档构造变体，复现首版工具放行实际步骤参数漂移、报告计数矛盾和缺失持久终态。标为 unavailable 且带数值的 RSS 也被误算为差值。隔离修正核对原始计数、持久终态、共同已执行的步骤配置及 RSS 覆盖，拒绝这些变体。原运行超时而候选执行更多步骤时仍保留失败与成功各自统计，未以失败用时计算成功性能差值。主控从首版脚本重新应用补丁，两组验证输出均与 F 逐字节一致。旧栅格归档仅能对照已确认选项和实际工具版本。正式逐步比较还需留存实际请求与固定执行绑定。补丁及完整命令位于 `artifacts/test-results/p13-05-memory-comparator-reviewed.MnDC2U/SHA256SUMS`。 [复核原始结果](p13-05-preflight-comparator-review.log)

F 的栅格采集增量在端到端墙钟停止、采样器退出后，只读获取适配器归一化步骤请求，保留工具、参数、状态及退出码。4 项专项通过。从真实归档副本核对 DEM S 的 14 个 run 共 98 条请求，以及 DEM L 和遥感 L 各一个超时 run 的 7 条和 8 条请求，均与遥测顺序和退出码一致。比较器按证据级别验证，接受同级对照，拒绝混用声明级与逐步请求级证据、同工具参数漂移及漏步。正式复测仍需固定或记录 QGIS provider 绑定与实际配置。两个补丁及复跑材料位于 `artifacts/test-results/p13-05-raster-step-requests.2FpacQ/SHA256SUMS`。 [命令、测试和归档复验](p13-05-preflight-raster-step-requests.log) · [JUnit](p13-05-preflight-raster-step-requests-junit.xml)

## 隔离提交与原生冒烟

| 用途 | 本地 Git 引用 | 提交 |
| --- | --- | --- |
| 前测源码及对照工具 | `codex/p13-05-preflight-base` | `977ab412d9f6e7558d22a5c431fac0ada90b07be` |
| 七项优化候选 | `codex/p13-05-preflight-candidate` | `e874a0f7510ad7d99e0e623f211e0f5afeb411cd` |

前测提交在 `b75090b` 上加入栅格请求采集、测试及前后比较工具，生产源码与应用配置逐文件相同。候选是前测的直接后继，两侧基准脚本、应用配置和依赖清单相同。候选的隔离回归通过 104 项，包含栅格采集测试。比较工具命令行帮助退出 0。 [测试日志](p13-05-preflight-source-candidate.log) · [JUnit](p13-05-preflight-source-candidate-junit.xml)

原版及候选在同一 Ubuntu 24.04.5 镜像中各执行一次 S 级 T13 线相交和 T14 按面积分配。四次均通过独立验算。T13 裁回 128 条线，保留 3,936 m。T14 核验 96 个对象、144 条分配记录和 35,200 m²。输入文件逐字节相同，归一化步骤参数相同。两侧 GeoPackage 均通过登记指纹与独立内容核验，文件指纹跨运行有差异。T14 的 CSV、JSON 指纹一致。 [探针原始汇总](p13-05-preflight-native-probe.json)

隔离提交与原生探针的 `src/`、`config/` 分别逐文件复核，原版 233 个文件、候选 234 个文件，路径及内容均吻合。原生环境提供 Python 3.12.3 和 QGIS 3.44.15，两个方法均经生产 Python CLI 执行。这四次为单次 S 冒烟，正式规模的运行分布仍待复测。完整命令、输入、样本和校验清单位于 `artifacts/test-results/p13-05-native-smoke.uPJ6d1/proof/`。 [源码校验](p13-05-preflight-source-verification.jsonl)

## 运行配置指纹探针

隔离采集模块读取实际方法、工具契约、QGIS 参数绑定和传感器配置，纳入方法发布台账与 Python pyproj 解析的数据根。本地实测覆盖 59 个应用配置文件和 16 个 PROJ 数据文件。配置搬目录后摘要相同。单独改变 QGIS 枚举绑定、道路方法参数、Landsat nodata、发布台账或 PROJ 数据库内容时，摘要均变化。数据库变体保留原 EPSG 版本。无效配置及选中的文件或目录符号链接被拒绝。两次隔离运行的 15 条记录在结果及指纹上吻合。 [复验原始记录](p13-05-preflight-runtime-config.jsonl)

候选补丁、采集脚本和校验清单位于 `artifacts/test-results/p13-05-runtime-config.Tq8YGO/`。正式接入需在计时外采集并复核，原版和候选共用同一采集实现。QGIS 与 GDAL 原生运行时及其数据目录仍需固定。本探针未执行 GIS 算法。

## 原生运行时身份与配置对照

主控在两次新启动的容器中获取逐文件指纹，两次原始 JSON 逐字节相同。范围包括 `/runtime` 的 6,738 个条目、QGIS 插件与资源、系统 GDAL 和 PROJ 数据。QGIS 三个已配置算法的参数定义分别从 `qgis_process --json help` 获取，覆盖 `native:buffer`、`native:extractbylocation` 和 `native:extractbyattribute`。原生 GDAL 3.8.4 通过 Python OSGeo 确认数据位于 `/usr/share/gdal`，原生 PROJ 9.4.0 的搜索顺序为用户目录、`/usr/share/proj`，探针的用户目录不存在。Python pyproj 与 Rasterio 使用各自轮包的 PROJ 数据。 [运行时指纹与算法参数](p13-05-preflight-native-identity.json)

探针与正在运行的 P13-03 容器使用同一镜像和三份只读绑定。两份 QGIS CLI 文件逐字节匹配原生运行环境记录及已有的 QGIS LTR Debian 包。镜像 `groundwater-v7-spatial-qgis:0.15.5` 只有本机镜像 ID，来源构建使用固定 Ubuntu 基础镜像、LTR 系统包和项目轮包。相同的本机镜像可复跑，跨机器交付还需归档该镜像及外部绑定。脚本、两次逐文件指纹与 Debian 包验证见 `artifacts/test-results/p13-05-native-identity.0YMBk6/SHA256SUMS`。探针未执行 GIS 算法。 [QGIS 包来源](../../baseline/ENVIRONMENT.md)

C 的隔离比较器增加 `--require-runtime-configuration`，要求两侧报告各有 `runtime-configuration.json`。比较器对实际读到的报告字节重算摘要，校验一次运行起止的配置捕获并核对两侧指纹。归档上的 26 项断言符合预期，含 4 次匹配和 22 次预期拒绝。旧报告仍可双侧比较，配置级别记为 `unrecorded`。测试中用归档报告和新采集的配置验证协议，历史运行的实际配置身份仍以历史记录为准。候选补丁和字段样例在 `artifacts/test-results/p13-05-runtime-comparator.DtRzWy/`，正式接入待 P13-03。 [隔离复跑](p13-05-preflight-runtime-comparator.log)

## 遥感 S 稀疏分类复验

A 从 P13-04 的真实遥感 S 冷进程首个计量 run 复制 192 × 192 分类 GeoTIFF，三版读取同一文件。原版、面积首版和完整候选各预热 2 次、实测 5 次，在同一进程轮换调用面积函数。三版报告逐项相等，包含 3 类各 6,144 格、18,432 个 NoData 和 16,602,074.59679892 m² 等积面积。独立面积校验通过。三种布局在 72 批内交替，完整候选检查 71 次，命中 0 次。 [原始计时、计数和报告](p13-05-preflight-remote-s.log)

三版函数墙钟中位数分别为 304.31、245.35、235.29 ms。这个样本未复现遥感 L 局部窗口中布局候选的轻微变慢，计时变化无法归因于布局复用。正式遥感 S/L 的端到端、步骤耗时和峰值资源仍待测。分类输入、归档 run 请求、脚本和校验清单在 `artifacts/test-results/p13-05-remote-s-probe.qPGkpT/`。

## 正式前后测范围

复测矩阵覆盖 12 个场景。矢量 M 包含点相交、线包含、按面积分配与道路 v2，矢量 L 包含点相交、线相交与按面积分配。另测 DEM S/M/L 与遥感 S/L 的完整方法。每侧每个场景均做冷热两组，每组 2 次预热、5 次计量，合计 336 次尝试，其中 240 次计量。同一对照使用同一物理盘、实际输入、软件环境、采样规则和独立验算。道路 v2 保留 QGIS 与专用点裁剪入口。遥感 S 单列 `calculate_change` 步骤和端到端结果。

原版全组超时时记录候选通过数、失败阶段和截止时资源。成功性能分位至少需要三次独立验算通过的样本，局部或缺失的资源读数保留覆盖状态。另设受控超时、取消与重启试验，在生产暂存已写入、成果发布前触发，在基准清理目录前盘点残留并验证租约恢复。详细矩阵及历史档案索引位于 `artifacts/test-results/p13-05-remote-s-probe.qPGkpT/MATRIX.md`。以上为正式复测安排，待 P13-03 签收后执行。
