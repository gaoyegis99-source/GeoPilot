# P3 道路样例分区计数工具

2026-11-11（Asia/Shanghai），Agent-F 提交 `REVIEW`。依据 V2.0 §8.5、§12.3.4 和[步骤执行协议](../../plans/01_ARCHITECTURE/07_STEP_PROTOCOL.md)。

`python-count-points-by-zone` v1 通过统一 `run_step` 启动受信本地实现 `geopilot.count_points_by_zone`。`targets` 确定裁回后保留的对象 ID，`original_targets` 提供归属所用的原始点几何，`zones` 提供分区。三个图层须具有匹配的已知 CRS 和有效几何。分区标识取 `zones.source_feature_id`，须唯一且非空。点图层中的同 ID 重复记录在水平几何一致时合并，有位置冲突或保留 ID 缺少原始点时步骤失败。[工具契约](../../config/tools/python-count-points-by-zone.yaml) · [Python 工具](../../src/geopilot/gis/tools/count_points_by_zone.py)

对于 `centroid`、`unassigned`、`count`，每个保留 ID 按原始几何质心查询覆盖它的分区，覆盖包括多边形边界。恰有一个分区覆盖时归入该区。零区或多区覆盖时进入未归属行。CSV 按分区 ID 排序，保留零计数分区，最后写入 `zone_id` 为空的未归属行。列为 `zone_id`、`assignment_status`、`count`、`source_feature_ids`，ID 明细为排序后的 JSON 列表。

## 实际验证

测试先行，实际写入临时 GeoPackage 后因缺工具配置退出 `1`，共 8 项失败。实现后补测多图层 GeoPackage 未指定图层，确认旧实现误用首层。接入既有 GeoPackage 图层选择校验后，专项命令退出 `0`，`9 passed in 8.62s`。JUnit 共 9 项、0 失败、0 错误、0 跳过。[专项测试](../../tests/runners/test_count_points_by_zone.py) · [JUnit](../../artifacts/test-results/p3-count-tool-junit.xml)

```bash
count_test_tmp=$(mktemp -d /tmp/geopilot-p3-count.XXXXXXXX)
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 PYTHONDONTWRITEBYTECODE=1 \
  python -m pytest tests/runners/test_count_points_by_zone.py -q -p no:cacheprovider \
  --basetemp "$count_test_tmp/pytest" --junitxml=artifacts/test-results/p3-count-tool-junit.xml
```

在另一独立临时目录合跑本专项、`tests/runners/test_clip_points_to_extent.py` 和 `tests/runners/test_python_runner.py`，退出 `0`，`32 passed in 16.80s`。

以 `-k real_golden` 保留的实跑记录退出 `0`，`1 passed, 8 deselected in 1.56s`。测试从 P0 样本按独立参考 ID 构造裁后及裁前点输入，再实际读取并统计。输入 GeoPackage 均以只读 SQLite 重开，`integrity_check=ok`，EPSG:32650，裁后点、原始点和分区要素数分别为 6、7、2。执行前后输入字节一致。实际分区结果如下。[P0 参考](../../tests/fixtures/vector/expected.json) · [CSV](../../artifacts/smoke/p3-count-tool/run.0XAPmRYQ/pytest/test_real_golden_point_count_m0/outputs/statistics.csv)

| 分区 | 计数 | 原始 ID |
|---|---:|---|
| `zone-west` | 3 | `building-buffer-boundary`、`building-edge-road`、`building-west` |
| `zone-east` | 2 | `building-east`、`building-overlap` |
| 未归属 | 1 | `building-zone-boundary` |

实际步骤为 `SUCCEEDED`，进程退出 `0`。环境为 Python 3.13.13、GeoPandas 1.1.3、Pyogrio 0.12.1、PyProj 3.7.2、Shapely 2.1.2。[请求](../../artifacts/smoke/p3-count-tool/run.0XAPmRYQ/pytest/test_real_golden_point_count_m0/steps/aggregate_by_zone/request.json) · [进程日志](../../artifacts/smoke/p3-count-tool/run.0XAPmRYQ/pytest/test_real_golden_point_count_m0/steps/aggregate_by_zone/process.log) · [子进程清单](../../artifacts/smoke/p3-count-tool/run.0XAPmRYQ/pytest/test_real_golden_point_count_m0/steps/aggregate_by_zone/python-result.json) · [步骤结果](../../artifacts/smoke/p3-count-tool/run.0XAPmRYQ/pytest/test_real_golden_point_count_m0/steps/aggregate_by_zone/result.json)

本证据覆盖独立的第七步。七步运行与复算由 P3 阶段验收执行，跨界面积等规则按 P6 验收。
