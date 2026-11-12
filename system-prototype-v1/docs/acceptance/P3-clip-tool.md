# P3 道路样例点目标裁回工具

Agent-B 建议 `REVIEW`。本工具用于 V2.0 §8.5、§12.3.4 的第六步 `clip_back_to_analysis_extent`，通过 P2-10 的 `run_step` 调用独立 Python 进程。[阶段计划](../../plans/03_STAGES/P3_通用方法执行链.md)

## 工具接口

`python-clip-points-to-extent` v1 绑定本机 `geopilot.clip_points_to_extent` 实现，接受选中的 `source` 点图层、原始 `extent` 面图层及 `output_scope=clipped_to_analysis_extent`。两图层必须有一致的已知 CRS。分析范围取所有有效面的并集，保留内部与边界点，包括孔边，剔除孔内与范围外的点。

`targets` 与 `original_targets` 分别写入独立的 GeoPackage。两份成果都保留入选行的 `source_feature_id`、原坐标及属性，重复点行由分区统计步骤后续去重。无入选目标时仍生成有 CRS、字段和 Point 几何声明的空图层。两个成果都完成暂存后才发布，目标文件按排他方式创建。

新增 `config/tools/python-clip-points-to-extent.yaml`、`src/geopilot/gis/tools/clip_points_to_extent.py`、`tests/runners/test_clip_points_to_extent.py`、本记录和测试证据。

## 实际验证

```bash
mkdir -p artifacts/smoke/clip-points
p3_clip_evidence=$(mktemp -d artifacts/smoke/clip-points/run.XXXXXX)
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest tests/runners/test_clip_points_to_extent.py -q \
  --basetemp="$p3_clip_evidence/pytest" --junitxml=artifacts/test-results/p3-clip-tool-junit.xml
```

| 验证 | 结果 |
|---|---|
| 实现前运行专项测试 | 工具契约文件缺失，`FileNotFoundError`，退出 `1`。 |
| 实现后专项命令 | `8 passed in 7.03s`，退出 `0`。[JUnit](../../artifacts/test-results/p3-clip-tool-junit.xml) |
| `python -m pytest tests/runners/test_clip_points_to_extent.py tests/runners/test_prepare_measurement_lines.py tests/runners/test_python_runner.py -q` | `24 passed in 11.48s`，退出 `0`。 |
| P0 `intersects` 前置选择 | 7 点裁为 6 点，两输出的 ID、WKB 和 CRS 与黄金样本一致。原输入指纹保持一致。[进程日志](../../artifacts/smoke/clip-points/run.rX6EEn/pytest/test_clip_points_from_golden_b0/steps/clip_back_to_analysis_extent/process.log) · [子进程清单](../../artifacts/smoke/clip-points/run.rX6EEn/pytest/test_clip_points_from_golden_b0/steps/clip_back_to_analysis_extent/python-result.json) |
| P0 `within` 前置选择 | 6 点裁为 5 点，两输出的 ID、WKB 和 CRS 与黄金样本一致。原输入指纹保持一致。[进程日志](../../artifacts/smoke/clip-points/run.rX6EEn/pytest/test_clip_points_from_golden_b1/steps/clip_back_to_analysis_extent/process.log) |
| 边界、孔洞、重复和空图层 | 边界及孔边纳入，孔内与范围外排除。保留重复点和原属性，空点层及无交集均产出两份可读 Point 图层。 |
| 失败结果 | CRS 不一致、未知 CRS 及第二份成果图层名被 GDAL 拒绝时，实际子进程退出 `1`，两份目标文件均未发布。 |

另用 `sqlite3` 以只读模式查询四份黄金结果的 `gpkg_geometry_columns` 与 `source_feature_id`，均为 `POINT`、EPSG:32650，ID 集合与参考一致。[P0 参考](../../tests/fixtures/vector/expected.json)

## 范围与限制

P3 的 `source` 限单点要素，`extent` 支持 Polygon 和 MultiPolygon。工具使用同一 CRS 做拓扑裁回，范围和要素均在本地进程读取。完整道路链、T07/T08 及分区计数由阶段集成验证。
