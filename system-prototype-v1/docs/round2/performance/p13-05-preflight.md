# P13-05 资源优化的正确性复验准备

P13-05 等待矢量基准验收。本轮增加面积回归用例和小样本对照证据，生产算法继续使用原实现。测试基于 `5ef9b042528c1f6b55461b9ad5dffa8e24317fe2` 加新增测试。Python 3.13.13、PROJ 9.5.1、GDAL 3.9.3、GEOS 3.13.1。

## 等积面积

- A 的粗剪切分类栅格在生产实现中细分至 256 段。固定八段的最大单类相对差为 `3.70e-7`。同一类别的 257 个像元样本产生两个批次，分别使用 64 段和 32 段。 [原始探针](p13-05-preflight-area-probe.log)
- 非零仿射行列式的 4 × 2 栅格仍出现零面积像元。生产逐格校验拒绝该输入，按类合并的边界积分却返回了正面积。反经线以及 EPSG:8858 的 90°E、EPSG:8859 的 30°W 切缝也触发现有投影范围校验。最初切缝试探的两个 EPSG 经纬位置互换，使用实际切缝位置的结果见复测。 [切缝与退化复测](p13-05-preflight-area-cuts.log)
- 主控在每个原批次内复用同一格点的投影值，仍按原顺序逐格计算和细分。15 个合成输入的报告或错误完全一致，三个真实分类栅格窗口的逐格面积数组和各批细分数完全一致。两个 DEM 窗口的变换输入各从 56,840 点降至 16,549 点，遥感窗口从 30,464 点降至 10,138 点。此记录采用同一数值路径比较点数，完整栅格耗时和内存需在生产候选上复跑。 [坐标复用探针](p13-05-preflight-area-reuse.log)
- A 在 `78b8174cb25a74ec74dc83db3ae3a5ccd09e15b4` 的独立副本实现批内投影坐标复用。18 项面积测试通过，8 个微型 GeoTIFF 的完整结果或错误与基线精确一致，输入指纹一致。密集 256 格样本的 PROJ 入点从 14,336 降至 4,097，收敛分段和逐格面积一致。隔离补丁、命令、JUnit、对照脚本和日志的校验清单保存在 `artifacts/test-results/p13-05-area-candidate.HU5Lhc/SHA256SUMS`。正式墙钟时间和峰值内存待测。

## 暂存文件生命周期

原生 P13-04 遥感 L 的 14 次超时均留下两份暂存栅格。主控用基线 `78b8174cb25a74ec74dc83db3ae3a5ccd09e15b4` 的 `capture_process` 启动真实 Python 子进程，调用实际 `_stage_outputs`，写入模拟差值和内层分类文件，再由父进程触发超时或取消。隔离候选由父进程在输出所在文件系统分配独占目录，等子进程退出后按目录身份回收。两次探针均退出 0。 [原始结果](p13-05-preflight-staging.log)

| 场景 | 子进程退出码 | 基线暂存文件 | 候选暂存文件 | 成果发布 |
|---|---:|---:|---:|---|
| 正常退出 | 0 | 0 | 0 | 成功 |
| 超时并响应 SIGTERM | -15 | 2 | 0 | 无 |
| 超时并由 SIGKILL 终止 | -9 | 2 | 0 | 无 |
| 取消 | -15 | 2 | 0 | 无 |

每次都核验输入和既有暂存目录中的标记文件原样保留。探针以 Python 3.13.13 在独立目录执行。脚本、候选助手、子进程补丁与原始 JSONL 保存在 `artifacts/test-results/p13-05-staging-preflight.B5CwoC/SHA256SUMS`。此探针验证父进程存活且工作进程终止后的清理。父进程崩溃后的重启回收及多文件发布中途被信号打断属于另两条待验证路径。生产调用链集成和完整遥感复测仍待 P13-03 验收。

## 矢量空间关系

正式 L 原版样本的 `apply_spatial_relation` 触发生产步骤时限。主控对真实 L GeoPackage 的 64 格窗口使用原并集、准备后的并集、索引筛选后局部并集三种口径。384 点、640 线、768 面的 `intersects` 和 `within` 选中 ID 均符合独立参考。该窗口每个相交目标只有一个候选，另用相邻覆盖面反例验证跨面目标需要局部并集。现有自动化已覆盖接缝和间隙。完整规模仍须按正常工具链复跑选择、裁剪、登记和独立结果核对。 [原始窗口输出](p13-05-preflight-t13.log) · [已有接缝测试](../../../tests/gis/test_within.py)

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
