# P0-05 道路缓冲与点分区统计黄金样本

`road-buffer.gpkg` 是人工构造的 15 个要素，坐标系为 EPSG:32650（WGS 84 / UTM 50N），水平单位为米。所有输入使用文本字段 `source_feature_id`。图层名对应 V2.0 §8.5 和附录 A.1 的方法输入角色。

| 图层 | 几何 | 数量 |
|---|---|---:|
| `analysis_extent` | Polygon | 1 |
| `roads` | LineString | 3 |
| `targets` | Point | 9 |
| `zones` | Polygon | 2 |

## 独立参考依据

分析区为 `[500000, 502000] × [4400000, 4402000]`。西区与东区在 `x = 501000` 相接。三条水平道路的 x 区间均在分析区内，因此与分析区的最短距离可直接取 y 间距。`road-main` 位于区内，`road-edge` 在北边界外 300 m，`road-far` 在北边界外 650 m。按 500 m 外扩范围取数，纳入 `road-main` 和 `road-edge`。

目标点与水平线段的距离由 x 到端点区间的距离和 y 到道路的距离平方和开根得到。下表坐标为从 `(500000, 4400000)` 起算的米数。

| 对象 | 坐标偏移 (x, y) | 参考结论 |
|---|---|---|
| `building-west` | (750, 1500) | 主线缓冲内，西区 |
| `building-east` | (1250, 1500) | 主线缓冲内，东区 |
| `building-overlap` | (1400, 1900) | 距主线和区外道路各 400 m，按原始 ID 在东区计数一次 |
| `building-edge-road` | (200, 1950) | 距 `road-edge` 350 m，距主线约 541 m，由区外道路纳入，西区 |
| `building-zone-boundary` | (1000, 1600) | 缓冲内，恰在共享分区边界，按 `boundary_tie: unassigned` 保留 |
| `building-buffer-boundary` | (750, 1000) | 主线缓冲直边上的点，`intersects` 纳入，`within` 排除 |
| `building-outside-extent` | (900, 2350) | `road-edge` 缓冲内，在关系判断后裁回时剔除 |
| `building-far-road` | (900, 3075) | 仅在已排除的 `road-far` 缓冲内，用于捕捉外扩取数范围错误 |
| `building-unaffected` | (500, 300) | 分析区内，离候选道路均超过 500 m |

`expected.json` 保存关系判断后与裁回后的精确 ID 集合，以及分区去重 ID。`intersects` 裁回后的计数为西区 3、东区 2、未归属 1，共 6；`within` 排除缓冲边界点后为西区 2、东区 2、未归属 1，共 5。除直边上的 `building-buffer-boundary` 外，各点距 500 m 临界值的余量都大于圆弧离散的最大弓高。点对象按所在分区计数。

### T03 单道路圆端缓冲

只取 `road-main` 的线段 `(500500, 4401500)` 到 `(501500, 4401500)`。长度 1000 m，半径 500 m，圆端与圆连接，16 段/四分之一圆。QGIS 对应 `DISTANCE=500`、`SEGMENTS=16`、`END_CAP_STYLE=0`、`JOIN_STYLE=0`。完整道路样例合并重叠缓冲，T03 的几何基准仅针对这条单线。[QGIS Buffer 参数定义](https://docs.qgis.org/3.44/en/docs/user_manual/processing_algs/qgis/vectorgeometry.html#buffer)

面积由矩形和两端半圆组成。两端离散后等价于一个 64 边形，面积是 `2rL + 32r² sin(π/32) = 1784137.122636485 m²`。理想连续面积 `2rL + πr² = 1785398.1633974481 m²`，离散圆弧最大弓高 `r(1 − cos(π/64)) ≈ 0.602272 m`。这个面积用于核对缓冲几何，产品面积统计需使用等积投影。

边界可用三角函数独立重建。右端帽角度从 `−π/2` 到 `π/2`，左端帽从 `π/2` 到 `3π/2`，两者均按 `π/32` 递增且保留端点。在各自线段端点上加 `(r cos(θ), r sin(θ))`，按右端帽、左端帽的顺序连接并闭合，得到两条直边和两组圆弧折线。包络为 `[500000, 4401000, 502000, 4402000]`。与此离散几何比较时，包络及边界 Hausdorff 距离容差为 `10⁻⁶ m`，面积容差为 `0.01 m²`。此距离余量高于本坐标量级的双精度舍入误差，沿约 5142 m 周长造成的面积差约 `0.0052 m²`，低于面积容差。

## 生成与复核

`source.json` 保存人工坐标、WKT 和 ID。构造器只负责序列化输入。运行需有 GeoPandas、Pyogrio、Shapely，输出目录需已存在，目标文件需是新路径。每次重建的逻辑内容一致，文件元数据可能随构建环境和时间变化。

```bash
vector_fixture_tmp=$(mktemp -d)
python tests/fixtures/vector/build_fixtures.py "$vector_fixture_tmp/road-buffer.gpkg"
TMPDIR="$vector_fixture_tmp" python -m pytest tests/golden -q
```

## P0-05 提交证据

- 执行人 Agent-F，状态 `REVIEW`。新增 `source.json`、`expected.json`、`build_fixtures.py`、`road-buffer.gpkg`、本说明、`tests/golden/test_vector_fixtures.py` 和 JUnit 证据。无公共接口或数据库契约变更。
- `python tests/fixtures/vector/build_fixtures.py tests/fixtures/vector/road-buffer.gpkg` 退出 0。`sqlite3 -readonly` 检查返回 `ok`，登记四个要素图层，`srs_id` 均为 32650。
- 用独立 GDAL 3.10.2 的 `/home/mtd/miniconda3/envs/liweigis/bin/ogrinfo -ro tests/fixtures/vector/road-buffer.gpkg` 重开成功，四个图层的几何类型分别为 Polygon、Line String、Point、Polygon。
- `python -m pytest tests/golden -q` 退出 0，`9 passed in 2.48s`。使用本次独立 `TMPDIR` 运行。
- `python -m pytest tests/golden/test_vector_fixtures.py -q --junitxml=artifacts/test-results/p0-05-junit.xml` 退出 0，`5 passed in 1.74s`，实测重新打开已交付与重建的 GeoPackage，核对全部几何、ID、CRS 和分析参考，验证同名输出拒绝覆盖。[JUnit](../../../artifacts/test-results/p0-05-junit.xml)
- T03、T07、T08 的生产链执行仍为 `NOT_RUN`。本任务未对全部 T13、T14 场景作验收。
