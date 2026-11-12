# P0-03 测地线道路缓冲预研

2026-11-11 核对 V2.0 §5.1、§12.3.1、§12.3.7 和 T04。P0 的真实缓冲使用 EPSG:32650 和 `native:buffer`，验证的是投影距离。测地线分支仍为 `NOT_RUN`，下表记录 P6-09 的候选入口和缺项。

| 入口 | 依赖、许可、输入几何 | 核对结果 |
|---|---|---|
| QGIS `native:buffer` | QGIS Processing，算法源码标注 GPL v2 或更新版本。支持点、线、面要素生成面缓冲。 | 3.44.15 源码调用 `QgsGeometry::buffer`，在输入坐标空间做平面计算。全局椭球选项不改变该实现。参见 [QGIS 3.44.15 算法源码](https://github.com/qgis/QGIS/blob/final-3_44_15/src/analysis/processing/qgsalgorithmbuffer.cpp)、[QgsGeometry API](https://api.qgis.org/api/classQgsGeometry.html)。 |
| QGIS Shape Tools | QGIS 插件，仓库含 GeographicLib，仓库许可文件为 GPL v2。`Create geodesic shapes` 从点要素生成图形，测地加密工具接受线和面。 | 已查阅的工具接口提供点中心圆和折线加密。完整的测地线道路缓冲仍缺逐线段侧边、端点圆弧和转角拼合。参见 [插件说明](https://plugins.qgis.org/plugins/shapetools/)、[输入和依赖说明](https://github.com/NationalSecurityAgency/qgis-shapetools-plugin#readme)、[许可文件](https://github.com/NationalSecurityAgency/qgis-shapetools-plugin/blob/master/LICENSE)。 |
| Python `pyproj.Geod` + 面构造 | PyProj 3.7.2（MIT）、PROJ 9.5.1（X/MIT）提供椭球正反算。需要自行构造折线缓冲。若使用 Shapely（BSD 3-Clause）做拓扑拼合，应先定义处理经度跨越和合适的拓扑空间。 | P0 实测 WGS 84 正反算。P6 可先实现单条 `LineString`，再用明确契约扩展至 `MultiLineString`。拼合和全线误差仍待实测。参见 [Geod 接口](https://pyproj4.github.io/pyproj/stable/api/geod.html)、[PyProj 许可](https://github.com/pyproj4/pyproj/blob/3.7.2/LICENSE)、[PROJ 许可](https://proj.org/en/stable/about.html)、[Shapely 许可](https://github.com/shapely/shapely/blob/main/LICENSE.txt)。 |
| ArcGIS Pro `arcpy.analysis.Buffer(..., method="GEODESIC")` | 依赖 Esri ArcGIS Pro/ArcPy 及适用许可，工具支持点、线和面，可选保形测地缓冲。 | 若可取得独立许可环境，可作为同输入的外部参考。P0 未运行 ArcGIS。参见 [Buffer 工具及许可级别](https://doc.esri.com/en/arcgis-pro/latest/tool-reference/analysis/buffer.html)、[测地线方法说明](https://doc.esri.com/en/arcgis-pro/latest/tool-reference/analysis/how-buffer-analysis-works.html)。 |

PostGIS `ST_Buffer(geography, ...)` 先选择投影，在平面缓冲后转回 WGS 84。P6 不应将这一接口登记为椭球线缓冲。参见 [PostGIS ST_Buffer](https://postgis.net/docs/ST_Buffer.html)。

## P0 最小实测

输入为人工固定的赤道点 `(117°, 0°)`。沿赤道向东 1000 m 后，WGS 84 赤道弧长给出解析经度 `117° + degrees(1000 / 6378137)`。在开发环境运行以下正反算检查，退出码 `0`，实测 `longitude=117.008983152841 latitude=0.000000000000 inverse_distance_m=1000.000000000`。

```bash
python - <<'PY'
from math import degrees, isclose
from pyproj import Geod

geod = Geod(ellps="WGS84")
lon, lat, _ = geod.fwd(117.0, 0.0, 90.0, 1000.0)
_, _, distance = geod.inv(117.0, 0.0, lon, lat)
expected_lon = 117.0 + degrees(1000.0 / 6378137.0)
assert isclose(lon, expected_lon, rel_tol=0.0, abs_tol=1e-12)
assert isclose(lat, 0.0, rel_tol=0.0, abs_tol=1e-12)
assert isclose(distance, 1000.0, rel_tol=0.0, abs_tol=1e-8)
print(f"longitude={lon:.12f} latitude={lat:.12f} inverse_distance_m={distance:.9f}")
PY
```

## P6 参考与未完成项

先固定顶点之间的线段语义、椭球、距离单位、沿线采样、圆弧离散规则、经度跨越处理和容差，再构造侧边及圆端。至少用直线、终点外侧探针和转角（内外侧）核对输出。可用 [GeographicLib 正反算](https://geographiclib.sourceforge.io/html/python/geodesics.html) 沿每段等距采样，求探针到样本点的最小椭球距离，另单独计算两个端点和折线转角。若采样最大间隔为 `h` 且包含每段两端，三角不等式将最小距离的采样高估限制在 `h/2`，可先验证离边界大于误差带的包含/排除，再加密复核临界点。PyProj 与 GeographicLib 的相关实现可共享测地线算法思路，因此还需保留赤道解析断言或独立软件对照。

完整道路缓冲的圆端、转角、线段侧边、拓扑拼合和 T04 参考文件均待 P6-09。当前可复用的是 WGS 84 计算入口、候选比较和参考设计。
