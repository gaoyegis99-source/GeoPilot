# 已接入方法独立回归

实际 QGIS 版本为 3.44.15。道路和属性过滤通过 QGIS 与已登记的 Python 工具执行，矢量和栅格成果均从本机持久化文件重新读取。面积容差以平方米表示。

| 已接入方法 | 输入与前提 | 参考结果与容差 | 失败输入与结果 | 端到端记录 |
|---|---|---|---|---|
| 道路缓冲统计 v2 | EPSG:32650 道路、AOI、点及分区，局部投影 500 m，相交和质心规则 | 300 m 外道路纳入、650 m 外道路排除。分区西 3、东 2、未归属 1，总数 6。目标 ID、数量和统计 CSV 精确比对，缓冲复算 Hausdorff 距离 ≤ 10⁻⁶ m、面积差 ≤ 0.01 m² | EPSG:32649 与实际分析数据 CRS 不一致时前置拒绝。T01/T02 另实跑平面测量 CRS 阻断 | [独立工作目录两次七步实跑](../../../tests/application/test_plan_run.py#L118)，[输入前置](../../../tests/application/test_method_preconditions.py)，[道路浏览器](../../smoke/p5-06/two-methods-browser.json) |
| 属性过滤导出 v1 | GeoPackage 目标图层包含 `source_feature_id`，按值等于 `building-west` 筛选 | 唯一结果 `building-west`，几何零容差且源字节不变 | 缺失字段 `missing_column` 返回带字段定位的前置错误；另实测成功执行后的独立检查失败 | [确认与执行](../../../tests/application/test_plan_run.py#L205)，[实际 QGIS 工具](../../../tests/runners/test_attribute_filter.py)，[输入前置](../../../tests/application/test_method_preconditions.py) |
| DEM 地形分析 v1 | UTM 10 m 原格、显式米制垂直单位、等积统计 EPSG:6933，坡度和坡向分别确认 | 9 个像元均为 45° 坡度或 270° 坡向，角值逐像元精确比较。总面积 900.7204322 m²，绝对容差 10⁻⁵ m² | 非等积目标在建 run 前被拒绝；无高度单位需明确选择 | [HTTP 七步、原始 TIFF、成果和报告](../../../tests/methods/test_dem_terrain_method.py)，[实际浏览器](../../smoke/p7-10/terrain-browser.json) |
| 土地利用变化 v1 | 两期有效面、AOI、分区及显式类别映射。独立叠加复跑覆盖同 CRS 与跨 CRS | 叠加四块 1,000、3,000、1,500、4,500 m²，面积及对称差容差 0.01 m²。正式方法应用 2,000 m² 阈值和同转移合并后保留三块，共 8,500 m² | 未知类别阻断映射；错误几何、非等积目标或超容差重叠/空洞不能产出合格结果 | [两期叠加](../../../tests/methods/test_land_use_overlay.py)，[七步矩阵及三归属](../../../tests/gis/test_transition_matrix.py)，[实际浏览器](../../smoke/p6-browser/land-use-browser.json) |
| 完整对象空间筛选与裁剪 v1 | 点、线或面，完整对象先对关系区使用 `intersects` 或 `within`，再向独立范围裁剪 | 六种组合的 ID 和几何拓扑精确相等，保留裁前对象，裁后形状严格处于裁剪范围 | 调换固定步骤返回 `LOCKED_STEP_ORDER` 并定位第一个步骤 | [两个实际子进程、快照、正式成果和检查](../../../tests/methods/test_full_geometry_before_clip.py#L126) |
| 图斑分区统计 v1 | EPSG:6933 图斑、分析区、分区、稳定来源 ID，三种跨界归属各自确认 | 6 个唯一对象，2,200 m² 总量、300 m² 未覆盖，逐来源和分区贡献精确匹配手算 | 同一 ID 的冲突原始图斑使统计进程退出并且无正式统计表；不同等积 CRS 在派发前拒绝 | [实际子进程、明细和面积](../../../tests/gis/test_source_feature_identity.py#L140) |
| 两期遥感指数变化 v1 | 原生 Landsat 与 Sentinel 产品打包，使用已配置精确版本、QA、AOI、0.25 变化阈值和 EPSG:6933 | 4 有效、11 NoData，变化类别 1/2/1 个。关键像元 11/17 容差 10⁻¹⁵，分类面积 10⁻⁵ m²、总面积 2×10⁻⁵ m² | 两种传感器未知版本均在运行前阻断；超范围 NDVI 有诊断且不污染对齐 | [HTTP 八步、原格、统计及规则](../../../tests/methods/test_remote_sensing_change.py)，[原生文件经工作台复算](../../smoke/p8-native-workbench-verified/browser.json) |
| 审阅注册的 Python 扩展示例 | 本地脚本、版本化工具、明确输入绑定，实际 PostGIS 两个本地快照版本 | 断开 PostGIS 后正式方法各自读出唯一来源 ID `1`，坐标 (10, 30) 和 (11.25, 30.75) 精确对比，核对正式文件指纹 | 另一次脚本退出码 7，不能产生可登记证据或替换同编号正式运行 | [真实脚本和 PostGIS 集成](../../../tests/integration/extensions/test_extension_acceptance.py) |

道路缓冲 v1 在目录中保留为结构样例，未接入执行。椭球缓冲已通过独立工具的 T04 验证，正式道路 v2 接入的是局部投影模式。[P6 阶段记录](../../../docs/acceptance/P6.md)

## 复跑命令

在原型目录执行，`GEOPILOT_QGIS_PROCESS` 指向已验收的本机 QGIS 入口。

```bash
export GEOPILOT_QGIS_PROCESS="$PWD/.environments/qgis-deb/qgis_process"
export PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 PYTHONDONTWRITEBYTECODE=1
python -m pytest tests/methods tests/application/test_plan_run.py \
  tests/gis/test_source_feature_identity.py tests/integration/extensions/test_extension_acceptance.py \
  -q -p no:cacheprovider --junitxml=artifacts/test-results/p11-03-methods-junit.xml
python -m pytest tests/application/test_method_preconditions.py tests/runners/test_attribute_filter.py \
  -q -p no:cacheprovider --junitxml=artifacts/test-results/p11-03-method-preconditions-junit.xml
python -m pytest tests/gis/test_transition_matrix.py \
  -q -p no:cacheprovider --junitxml=artifacts/test-results/p11-03-land-use-junit.xml
```

分别通过 110、3 和 7 项，共 120 项，零失败、错误或跳过。运行时分别为 213.59、22.24 和 41.27 秒。第一组有一条 Starlette TestClient 的 AnyIO 弃用提示。[方法 JUnit](../../test-results/p11-03-methods-junit.xml) · [前置条件 JUnit](../../test-results/p11-03-method-preconditions-junit.xml) · [土地利用 JUnit](../../test-results/p11-03-land-use-junit.xml)

本项新增方法回归报告、三份 JUnit 和签收记录。复用已验收的生产配置、样本和测试入口，无生产接口、Schema 或 SQLite 改动。
