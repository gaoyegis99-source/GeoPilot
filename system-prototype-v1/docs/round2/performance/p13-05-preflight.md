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

重启验证使用隔离回执和真实工作进程租约。当前候选的回执写入前中断可能留下空目录，写入前尚未启动子进程。正式启动恢复集成、多文件发布中断及完整遥感复测待 P13-03 验收后执行。

## 矢量空间关系

正式 L 原版样本的 `apply_spatial_relation` 触发生产步骤时限。主控对真实 L GeoPackage 的 64 格窗口使用原并集、准备后的并集、索引筛选后局部并集三种口径。384 点、640 线、768 面的 `intersects` 和 `within` 选中 ID 均符合独立参考。该窗口每个相交目标只有一个候选，另用相邻覆盖面反例验证跨面目标需要局部并集。现有自动化已覆盖接缝和间隙。完整规模仍须按正常工具链复跑选择、裁剪、登记和独立结果核对。 [原始窗口输出](p13-05-preflight-t13.log) · [已有接缝测试](../../../tests/gis/test_within.py)

A 的隔离候选在现有范围并集上预构建 GEOS 索引，沿用 `intersects` 和反向 `contains` 的 DE-9IM 语义。实际 L 三面小窗的点、线、面两种谓词共六次 CLI 执行均退出 0，与独立参考的 12/3、24/9、30/12 个结果相符。顺序、索引、属性、CRS 和 WKB 在新旧对照中一致。另有 6,840 次旋转、尺度、孔洞与多部件谓词对照，结果一致。单补丁 29 项通过，与点裁剪补丁隔离合并后 45 项通过。补丁、原始命令和 JUnit 保存在 `artifacts/test-results/p13-05-relation-candidate.MPdADO/SHA256SUMS`。完整 L 端到端性能待测。

L 档点相交的七次冷进程采样均在 29.496–30.517 秒完成 `apply_spatial_relation`。下一步将 65,536 个已筛选点裁回 16,384 个面覆盖范围，`clip_vector_to_extent.py` 均达到生产 60 秒时限，退出 -15。七次暖进程采样同样在裁剪超时。原始记录保存在 `/mnt/datab/geopilot-p13-03.KXUnnZ/L-remaining/evidence/samples.jsonl`。 [七次冷进程遥测和输入检查摘录](p13-05-preflight-t13-clip.log)

A 的隔离候选对确认是 XY 的 GeoPackage 点和面复用已准备的 `covers`。以真实 L 的三面、十八点窗口复跑原版与候选 CLI，先传入关系筛选后的 12 点，再直接传入 18 点。四次执行均退出 0，两套输出图层各保留相同的 12 个 ID、顺序、属性、CRS 和 WKB。首轮探针传绝对路径被现有协议拒绝，使用运行目录相对路径后完成对照。另对 1,224 个边界和邻近点比较掩膜及结果 WKB，完全一致。多点跨界和贴 Z 面边界的反例继续走现有相交运算，候选保留声明含 Z/M 的图层和空结果的原路径。16 项专项通过。源码补丁、原始日志和 JUnit 保存在 `artifacts/test-results/p13-05-point-candidate.pmbuVp/SHA256SUMS`。全量时间与峰值内存仍待测。

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
