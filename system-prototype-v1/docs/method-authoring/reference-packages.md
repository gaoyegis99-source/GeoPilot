# 方法样例包与参考输出

每个样例包固定一个方法版本和一个正例，可附多个失败用例。包由 `manifest.json`、可注册的输入文件和独立编写的预期结果组成。发布样例时，在 `basis` 中记录手算、解析推导或独立工具及其版本。预期文件与被测方法的实现分别维护。运行时复制输入到新工作目录，保留原始包和每个运行结果。

| 字段 | v1 约定 |
|---|---|
| `schema_version` | 整数 `1` |
| `case_id` | 正例 ID |
| `method` | V1 精确 `id` 和正整数 `version` |
| `environment` | 独立参考验证所用环境的非空字符串映射。记录适用的 Python、QGIS、GDAL、PROJ 等实测版本，报告另列当前观测值 |
| `inputs` | 输入角色映射。每项含相对 `path`、原始文件字节的 `sha256` 和可选 `layer` |
| `critical_options` | 提交给 V1 计划草稿的关键选项映射 |
| `outputs` | 正例完整成果角色映射。每项含 `kind`、`reference`（`path`、`sha256`）和独立参考的 `basis` |
| `failure_cases` | 可选失败用例数组。每项含独立 `id`、`inputs` 和/或 `critical_options` 的覆盖项，以及预期的 `expected.phase`、`expected.code` |

失败用例按角色浅合并输入，覆盖项必须替换正例中同名角色的整个文件声明。关键选项按顶层键浅合并，可新增选项或替换原有值，嵌套对象须完整提供。保留未覆盖项。阶段为 `draft` 或 `confirm`，代码使用该阶段 V1 返回的 issue code。确认拒绝后应查验任务的运行记录为空。没有为失败用例定义输出比较。

ID 和角色以 ASCII 字母或数字开头，后续字符可包含点、短横线和下划线。文件路径相对各自根目录，采用正斜杠分隔 ASCII 字母、数字、点、短横线或下划线组成的路径段。拒绝绝对路径、`.`、`..`、空路径段、符号链接和边车文件。输入支持 GeoPackage、GeoJSON 和 GeoTIFF。GeoPackage 需要关闭事务并使用 rollback journal 模式。参考文件为 UTF-8 JSON，SHA-256 指纹由 64 个小写十六进制字符组成。

## 正例比较

**矢量 `kind: vector`** 的参考包含 `crs`（EPSG）、唯一字符串 `id_field`、`attribute_fields` 及以 ID 为键的 `features`。各要素包含 `wkt` 和 `properties`。验证图层 CRS、要素总数、完整字段集合、ID 唯一性、按 ID 匹配的标量属性及有效二维几何的拓扑相等性。几何不设坐标容差，行顺序、环起点、环方向和二进制编码不影响比较。多图层成果须显式给出图层名。

**栅格 `kind: raster`** 的参考包含 `crs`（EPSG）、六项仿射 `transform`（`a,b,c,d,e,f`）、`resolution`、`width`、`height`、各波段的 `dtypes` 和 `nodata`。NoData 用数字、`"nan"` 或 `null` 表示。按波段、行、列编排 `pixels`，预期掩膜像元写 `null`。精确比较网格元数据和掩膜，仅对未掩膜数值使用 `absolute_tolerance`，相对容差为零。以 `tolerance_basis` 说明误差来源或精确值的依据。v1 参考面向小样本，所有波段合计最多 2,000,000 个内联像元。

**JSON `kind: json`** 的参考包含 `value` 和可选的 `numeric_tolerances`。对象键集合、数组顺序、标量类型和数值默认精确匹配。只有以 JSON Pointer 明确声明的数字可以使用绝对容差，每项含 `absolute` 和 `basis`。容差项允许 JSON 整数或浮点数，布尔值仍严格区分。重复键和非有限数值视为无效输入。

## 校验接口

```python
from pathlib import Path

from scripts.methods.reference_package import OutputFile, compare_outputs, load_reference_package

package = load_reference_package(Path("tests/method_dev/fixtures/attribute-filter/manifest.json"))
features = package.resolve_input("features")
options = package.case_options()
report = compare_outputs(
    package,
    {"filtered_features": OutputFile("intermediate/filter_features/filtered.gpkg", layer="filtered")},
    workspace=Path("path/to/run"),
)
```

加载时验证清单、所有正例和失败输入、参考文件的结构与指纹。`case_inputs(case_id)`、`case_options(case_id)` 和 `resolve_input(role, case_id)` 可指定失败 ID。`resolve_input` 每次读取前重验文件 SHA-256。调用方使用清单中的图层选择，提交选项前可用 V1 正式方法契约核对角色集合和类型。

`compare_outputs` 接受相对 `workspace` 的成果 URI 和可选图层，核对精确角色集合并拒绝样例目录内的文件或指向包文件的硬链接。返回 JSON 可序列化的 `schema_version`、`case_id`、`status`、逐角色 `outputs` 和 `issues`。成功项含成果 SHA-256 及适用的要素或像元计数。差异项含类型化的 `code`、`field` 和 `message`。`ReferencePackageError` 用于清单、参考文件或工作目录本身无效。校验器以只读方式打开文件，并在比较前后重新验证包指纹。运行报告同时记录清单声明和实际运行环境，以便重放时核对。

## 正式 V1 样例

- `attribute-filter-export@1` 选择两个手工定位的 EPSG:32650 点。输入要素顺序与参考顺序不同。缺少 `class_name` 的输入在确认阶段返回 `METHOD_PRECONDITION_FAILED`。 [样例包](../../tests/method_dev/fixtures/attribute-filter/manifest.json)
- `dem-terrain-analysis@1` 使用已有 V1 黄金 DEM 的人工平面。保留原像元值与间距，在 EPSG:6933 构造等积算例。内裁剪包含 25 个像元，两个像元的 3×3 邻域触及源 NoData。其余 23 个像元有每 10 米水平距离上升 10 米的坡度，预期为 45°，闭区间 [45°,45°] 将其划为类别 7，等积面积为 2300.0 m²。未标注高程单位的输入在草稿阶段返回 `DEM_VERTICAL_UNIT_REQUIRED`。输入构造脚本可在独立目录重建两个 DEM 文件。 [样例包](../../tests/method_dev/fixtures/dem-terrain/manifest.json)
