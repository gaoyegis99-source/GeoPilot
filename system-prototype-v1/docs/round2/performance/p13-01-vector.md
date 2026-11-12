# P13-01 向量规模生成器

- 执行角色为 B。状态 REVIEW，待主控签收。任务开始时工作树提交为 `9a862cd8d445827e3daf647134989a52f64ff1c1`。最终生成器和测试文件的 SHA-256 记录在复核摘要。 [机器摘要](p13-01-vector-summary.json)
- 生成器复用 P12-11 锁定的向量配置及 V1 道路、T13 合成坐标和参考清单。T14 的矩形、重复来源 ID 和三种分配参考来自 V1 固定样本的手工定义。输入只读，生成前后的输入哈希一致。 [配置](../../../benchmarks/datasets/vector.yaml) · [V1 道路参考](../../../tests/fixtures/vector/expected.json) · [V1 T13 参考](../../../tests/fixtures/vector/t13/expected.json) · [V1 T14 样本](../../../tests/gis/test_source_feature_identity.py)
- 环境为 Linux 5.15.0-139-generic x86_64，Python 3.13.13，GeoPandas 1.1.3，NumPy 2.4.4，Pyogrio 0.12.1，GDAL 3.11.4，Shapely 2.1.2，GEOS 3.13.1，PROJ 9.5.1。

## 输入与独立参考

`road.gpkg` 含 `analysis_extent`、`roads`、`targets`、`zones` 四层，`relations.gpkg` 含 T13 的 `area`、`points`、`lines`、`polygons` 四层，两者为 EPSG:32650。`t14.gpkg` 含 `targets`、`relation_area`、`analysis_extent`、`zones` 四层，使用 EPSG:6933。T13 保留内部环及精确接触点。T14 保留 V1 三段分离的裁剪范围、间隙、同 ID 的重复面、边界接触和未覆盖对象。

单元原点由对应基准原点加上行列索引乘 4,000 m。单元 ID 如 `r000c000`，要素 ID 为 `{tile_id}/{template_id}`。属性列含原始 `template_id`、`tile_id` 和固定长度 ASCII `bench_payload`。道路按档位共线加点，保留原端点及测量距离。生成器按批写入 GeoPackage，逐层重开核对 CRS 和记录数。

每个新输出目录含三个输入 GeoPackage 和 `manifest.json`。清单字段 `version`、`family`、`tier`、`files` 供统一调用，`files` 各记录相对路径、字节数和 SHA-256。参考对象包含各文件 CRS、单元与参考坐标原点、每层计数、属性与坐标的逻辑 SHA-256 以及独立预期结果。逻辑摘要依据按要素 ID 排序的属性和 little-endian WKB。文件时间戳固定，跨 GDAL 或 SQLite 构建时以逻辑摘要和实际内容核对为准。 [GDAL GeoPackage 文档](https://gdal.org/en/stable/drivers/vector/gpkg.html#creation-issues)

| 规模 | 单元数 | 每路顶点 | 属性字节 | GeoPackage 总行数 | GeoPackage 总字节 |
|---|---:|---:|---:|---:|---:|
| S | 16 | 2 | 16 | 928 | 696,320 |
| M | 1,024 | 9 | 64 | 59,392 | 18,694,144 |
| L | 16,384 | 33 | 256 | 950,272 | 522,145,792 |

- 道路参考保留候选道路、裁回前后 ID、分区和未归属计数。每单元 `intersects` 选 6 个目标，`within` 选 5 个。解析离散圆端胶囊记录 WKT、面积、边界和 V1 容差。
- T13 用 V1 ID 真值表和手写裁回坐标记录类型、保留线长和面积。点、线、面每单元 `intersects` 的计数为 4、8、10，`within` 为 1、3、4。
- T14 记录实际选取和裁回行数、重复 ID 次数、各 ID 的裁后几何，以及三种分配策略的逐 ID/区明细。每单元为 6 个唯一对象、2,200 m² 总面积和 300 m² 未覆盖面积。

## 命令与证据

在原型根目录执行。实际两轮使用独立临时目录中的不同输出子目录，命令、时间与退出码见原始日志。

```bash
set -o pipefail
scratch=$(mktemp -d /tmp/geopilot-p13-01-vector.XXXXXXXX)
export PYTHONDONTWRITEBYTECODE=1
for tier in S M L; do
  /usr/bin/time -p .venv/bin/python scripts/bench/generate_vector.py \
    --tier "$tier" --output "$scratch/$tier-replay" || exit $?
done
for tier in S M L; do
  /usr/bin/time -p .venv/bin/python scripts/bench/generate_vector.py \
    --tier "$tier" --output "$scratch/$tier-final" || exit $?
  cmp "$scratch/$tier-replay/manifest.json" "$scratch/$tier-final/manifest.json" || exit $?
done
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -m pytest -q -p no:cacheprovider \
  tests/benchmarks/test_generate_vector.py \
  --junitxml=docs/round2/performance/p13-01-vector-junit.xml
```

- 两轮 S、M、L 各生成命令退出 0。所有对应清单逐字节相等，记录的三个文件 SHA-256 也逐档相等。最终版本独立重开九个文件，复核各层 CRS 和数量、文件哈希、最后一单元的道路顶点和属性长度、T13 孔洞及 T14 重复 ID。 [第一轮日志](p13-01-vector-tiers.log) · [复跑日志](p13-01-vector-replay.log) · [重开与哈希摘要](p13-01-vector-summary.json)
- 最终专项 pytest 退出 0，10 通过、0 失败、0 错误、0 跳过，耗时 15.15 秒。覆盖真实 GeoPackage 重开、逻辑摘要重算、道路及 T13 边界判定、T14 实际分配、字节级重放、M 档扩展、三种已有目标拒绝和坏模板拒绝。 [JUnit](p13-01-vector-junit.xml) · [pytest 日志](p13-01-vector-pytest.log)

## 接口影响与回滚

新增 `scripts/bench/generate_vector.py`、`tests/benchmarks/test_generate_vector.py` 和本项报告及原始证据。公开入口为 `python scripts/bench/generate_vector.py --tier S|M|L --output NEW_DIRECTORY`，可选 `--root`。父目录必须存在，输出目录必须新建。文件先写入同文件系统的临时目录，清单最后发布。未改生产算法、配置、API、Schema 或数据库。复核后已清理约 1.1 GiB 的本项临时生成物，可由命令重建。

命令日志耗时为本次生成检查的运行记录。P13-03 另按重复、冷暖和资源采样协议形成性能基线。需回滚时可撤销主控集成提交中的本项生成器、测试和证据。
