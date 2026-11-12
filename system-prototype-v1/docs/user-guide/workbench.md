# GeoPilot 工作台操作

先启动本机后端和前端，再打开工作台。[安装与启动](../../README.md#启动工作台)

## 道路缓冲统计样例

1. 在资产区输入 `tests/fixtures/vector/road-buffer.gpkg` 的绝对路径。选择「读取图层」，依次选择并登记 `analysis_extent`、`roads`、`targets`、`zones`。图层分别表示分析范围、道路、目标点和统计分区。
2. 选择「道路缓冲统计」版本 3，填写任务目标，为四个角色选取同名资产版本。复验局部投影时填入下表参数。既有版本 2 的确认方案仍可按原版本运行。
3. 点击「生成草稿」，在方案区核对输入版本、距离口径和七步顺序。点击「确认方案」，再点击「开始运行」。
4. 等待执行和检查分别显示 `SUCCEEDED` 与 `PASSED`。在成果区下载缓冲面、选中目标与分区 CSV，或点击「地图定位」。预期共 6 点，西区 3、东区 2、未归属 1。[样本的独立参考](../../tests/fixtures/vector/README.md)

| 选项 | 样例值 |
| --- | --- |
| 缓冲距离 `distance_m` | 500 米 |
| 距离方法与 CRS | `local_projected`，`EPSG:32650` |
| 空间关系 | `intersects` |
| 分区方式与并列规则 | `centroid`，`unassigned` |
| 统计与输出范围 | `count`，`clipped_to_analysis_extent` |

### 椭球测地线模式

在「距离计算方式」选择「测地线」。工作台会隐藏局部投影 CRS 输入，方案预览中外扩、道路筛选、道路准备和缓冲步骤会显示对应工具。距离使用米，当前 WGS84 工具支持 1–2000 米。数据登记时使用原有 CRS，流程会准备 WGS84 分析区、目标、分区和候选道路。检查记录保存实际距离方式和工具版本。

可生成包含 WGS84 与 UTM 图层的赤道样本，登记四个无后缀的 WGS84 图层。500 米、`intersects` 的参考结果为西区 3、东区 2、未归属 1。改为 `within` 时，西圆端边界点排除，参考总数为 5。

```bash
.venv/bin/python tests/fixtures/geodesic/road_workflow.py /absolute/path/road-equatorial.gpkg
```

运行区可查看当前步骤和真实子进程日志。成果区的「下载原文件」返回正式文件。对栅格，地图支持显隐、定位、换色和点击原像元。检查区展示规则明细与历史报告。修改输入或关键选项后，生成新草稿并确认相应版本。

## 筛选任务候选资产

选定方法并填写任务目标后，工作台按角色刷新候选。有分析范围角色的方法使用该角色提供范围。其他支持空间筛选的方法可选择「候选筛选范围」。允许多种几何的角色可选「对象几何」，也可用「预期成果（候选筛选）」聚焦产生该成果的输入。固定步骤所需的角色继续显示。

更换范围或字段时，工作台保留已经选中的资产版本，并显示缺 CRS、范围不交叠或缺少字段等原因。候选筛选范围用于选择资产，分析的裁剪与覆盖仍由方法输入和参数决定。生成草稿会保存当前上下文。

## 其他方法

| 方法 | 准备与选择 |
| --- | --- |
| 属性筛选与导出 | 登记一个点图层，指定字段与匹配值。道路样例可用 `targets`、`source_feature_id`、`building-west`。 |
| 空间筛选与裁剪 | 登记目标、关系判定范围和裁剪范围，选择 `intersects` 或 `within`。 |
| 跨区图斑统计 | 登记图斑、统计分区和分析范围，选择按面积拆分、质心或最大重叠及适用的等积投影。 |
| 土地利用变化 | 登记两期分类、分区和分析范围，确认两期分类映射、变化面积阈值、分区归属与等积投影。 |
| DEM 地形分析 | 登记 DEM 和分析范围，指定坡度或坡向及阈值。DEM 需覆盖计算邻域，方案记录单位换算。 |
| 两期 NDVI 变化 | 登记两期原 DN 产品 GeoTIFF 和分析范围。确认时相顺序、重采样方式、变化阈值和等积 CRS。差值方向为第二期减第一期。 |

## 打包原生遥感产品

在启动后端的原型根目录执行。将 XML 参数替换为已解包产品内的实际文件路径，选择新 TIFF 目标。打包器从 XML 引用读取本机波段，保留原 DN、原元数据和来源记录。Sentinel-2 的 SCL 由 20 米最近邻对齐至 10 米。多 granule 产品用 `--granule-id` 选择确切项。[打包验证与格式](../acceptance/P8-10-product-pack.md)

```bash
GEOPILOT_DATA_DIR="${GEOPILOT_DATA_DIR:-$PWD/local-data}"
mkdir -p "$GEOPILOT_DATA_DIR"
PYTHONPATH="$PWD/src" .venv/bin/python -m geopilot.remote_sensing.product_pack \
  /absolute/path/landsat-product_MTL.xml "$GEOPILOT_DATA_DIR/first-product.tif" \
  --profile landsat-c2-level-2 --config-dir "$PWD/config/sensors"

PYTHONPATH="$PWD/src" .venv/bin/python -m geopilot.remote_sensing.product_pack \
  /absolute/path/sentinel-product.SAFE/MTD_MSIL2A.xml "$GEOPILOT_DATA_DIR/second-product.tif" \
  --profile sentinel-2-level-2a --config-dir "$PWD/config/sensors"
```

把输出 TIFF 分别登记为第一期和第二期。以下五份配置使用确切传感器、产品级别和处理版本查找。

| 产品 | 级别 | Collection 或处理基线 | 配置版本 |
| --- | --- | --- | --- |
| Landsat 8 OLI/TIRS | L2SP | `02` | `landsat-8-c2-l2sp@1` |
| Landsat 8 OLI/TIRS | L2SR | `02` | `landsat-8-c2-l2sr@1` |
| Landsat 9 OLI/TIRS | L2SP | `02` | `landsat-9-c2-l2sp@1` |
| Landsat 9 OLI/TIRS | L2SR | `02` | `landsat-9-c2-l2sr@1` |
| Sentinel-2 MSI | L2A | `05.11` | `sentinel-2-l2a-pb05-11@1` |

未知配置会在确认前定位到对应产品。两期 NDVI 和质量掩膜保留各自原网格，变化栅格使用第一期格网和共同有效像元。成果报告提供配置引用、定标系数、对齐和等积面积统计。[遥感方法验证](../acceptance/P8.md) · [五份配置](../../config/sensors/)

## 可选入口

- **本机模型。** 已有 Ollama 服务与模型时，在后端启动命令末尾增加 `--ollama-model <本机模型名> --ollama-port 11434`。在「模型辅助」填写目标，读取建议并复核数据、方法和参数，再生成草稿和确认。可以随时继续手工填写。[模型入口验证](../acceptance/P9.md)
- **PostGIS。** 在 Python 环境安装 `psycopg[binary]==3.3.4`。用 `PostGISSource` 描述本机连接别名、表、几何列和筛选，再调用 `import_postgis_snapshot` 把读出的 GeoPackage 登记到工作台使用的 SQLite。运行读取登记的本地快照。重新导入用 `refresh_postgis_snapshot` 和新目标文件，旧确认方案保留原版本引用。[导入与版本接口](../acceptance/P10-05.md) · [重新导入验证](../acceptance/P10-06.md)
- **本机脚本。** 用脚本运行接口保存并执行本机脚本快照，在工作台「脚本运行」查看独立日志、下载和地图预览。脚本目录位于 `workspace/scripts/<script_run_id>/`。经人工核对样例后，可将脚本收录为正式工具。[脚本运行与收录](../acceptance/P10.md)
