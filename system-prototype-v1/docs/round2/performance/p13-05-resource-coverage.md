# P13-05 已封存配对的资源复算

主控与 B 以 `f4e99073f8767513010fb719cbaf6f79a5b40416` 中的七份归档复算。覆盖 M 矢量四个负载、L 点相交、L 线相交、L 按面积分配、DEM S、遥感 S 重测、DEM L 共 10 个正式配对、20 个冷热组。每侧每组含两次预热和五次正式计量，共 280 个唯一运行。复算与严格比较报告中的正式状态计数、RSS 覆盖和分位逐组一致。 [脚本](p13-05-resource-coverage.py) · [分组统计和原始记录摘要](p13-05-resource-coverage.json) · [B 的独立复算与原始日志](p13-05-resource-coverage-independent.tar.gz)

| 200 次正式计量 | 样本数 | 复核结果 |
| --- | ---: | --- |
| 基准通过 | 137 | 持久终态为 `SUCCEEDED/PASSED`，已执行独立成果验算 |
| 状态观察失败 | 3 | 均为遥感 S 重测原版，持久终态和独立成果验算通过 |
| GIS 执行失败 | 60 | 持久终态为 `FAILED/NOT_CHECKED` |
| CPU、磁盘读与写 | 每项 200 | 均为已采样累计下界，至少三条读数时报告中位数和 P95 |
| 整轮工具子进程 RSS | 75 完整、125 部分 | 20 个冷热组均缺符合比较条件的成功样本 RSS 差值 |

严格报告允许比较成功端到端耗时的 10 个冷热组中，9 组中位数下降。M 档道路冷态增加 0.164 秒，P95 降低 0.451 秒。临时空间有 6 个冷热组可计算成功样本差值，其中 M 档暖态按面积分配的临时空间 P95 增加 942,218 字节。L 点相交及线相交原版均失败、修正版均通过。L 按面积分配冻结配对与 DEM L 两侧均失败。遥感 S 重测原版冷／暖分别通过 3／4 次，修正版各 5 次，成功样本差值为空。 [M 矢量](p13-05-vector-m-revised.md) · [L 矢量](p13-05-vector-l.md) · [栅格 S](p13-05-raster-s.md) · [栅格 M/L](p13-05-raster-m-l.md)

CPU、磁盘和 RSS 取自本地工具子进程的观测。磁盘读取的观测中位数为零时，仍只代表已采样范围。矢量输入字节取所选 GeoPackage 大小，栅格使用基准的输入文件总量并另保留逻辑输入字节。输出字节为已记录的成果文件大小，失败行缺失的输出字节保留为空。遥感 S 原版三条状态观察失败在冻结严格比较中计为 `unspecified`，补充统计用原始异常类型识别，并保留其基准失败标签。 [遥测口径](p13-02.md)

B 核对 M 档冷态按面积分配的 `aggregate_by_zone` 步骤，两侧各五条 RSS 采样均完整。原版到修正版的 RSS 中位数由 160,382,976 降至 160,165,888 字节，P95 由 160,616,448 降至 160,305,152 字节。这个结果只覆盖该步骤。280 个运行中，196 个完成独立成果验算，84 个因步骤超时未达到完整成果验算。

主控以 Linux 5.15、Python 3.13.13 运行以下命令，退出 0。另在内存副本中修改 DEM L 原版一个正式样本的状态，或给该样本 RSS 加 1,000,000 字节。两处修改分别触发计数和 RSS 校验拒绝，未改归档。本次统计固定于这十个配对。遥感 L 后续配对的资源观测已在原始记录中复算，DEM M 和 T14 后续候选继续。 [遥感 L 正式配对](p13-05-raster-m-l.md#遥感-l-正式配对)

```bash
p13_resources=$(mktemp)
.venv/bin/python -B docs/round2/performance/p13-05-resource-coverage.py docs/round2/performance > "$p13_resources"
cmp "$p13_resources" docs/round2/performance/p13-05-resource-coverage.json

p13_resources_review=$(mktemp -d)
tar -xzf docs/round2/performance/p13-05-resource-coverage-independent.tar.gz -C "$p13_resources_review"
(cd "$p13_resources_review" && sha256sum -c SHA256SUMS)
.venv/bin/python -B "$p13_resources_review/audit.py" f4e99073f8767513010fb719cbaf6f79a5b40416 \
  "$p13_resources_review/replayed.json"
cmp "$p13_resources_review/replayed.json" "$p13_resources_review/independent.json"
```

## 遥感 L 与 T14 局部裁回配对补充

主控以 `f105388d11106cc2d866cafacafc9a632579d3f5` 中的归档复算新封存的遥感 L 和 T14 局部裁回配对。12 个正式配对有 322 个唯一运行和 230 次唯一正式计量。T14 局部裁回复用的原版 14 轮与首次冻结配对逐字节一致，汇总只计一次。 [补充逐组统计](p13-05-resource-coverage-l-followups.json) · [遥感 L 原始配对](p13-05-remote-l-pair-evidence.tar.gz) · [T14 原始配对](p13-05-t14-local-clip-pair-evidence.tar.gz)

| 230 次唯一正式计量 | 数量 | 复核结果 |
| --- | ---: | --- |
| 基准通过 | 152 | 均为 `SUCCEEDED/PASSED` |
| 状态观察失败 | 8 | 均为遥感，GIS 成果和独立验算通过 |
| GIS 执行失败 | 70 | 均为 `FAILED/NOT_CHECKED` |
| CPU、磁盘读与写 | 每项 230 | 均为已采样累计下界 |
| 整轮 RSS | 78 完整、152 部分 | 24 个冷热配对组的成功样本 RSS 差值均为空 |

遥感 L 两侧的正式计量中，原版 10 次均超时，候选 5 次基准通过、5 次状态观察失败，候选 10 次均通过独立成果验算。T14 局部裁回新增的 10 次正式计量全部通过。复算校验每侧的正式状态计数、RSS 覆盖及分位。另在内存副本中将候选运行 ID 改为已封存的原版 ID，或给遥感 L 暖态通过样本的 RSS 增加 1,000,000 字节。两种篡改分别触发重复运行和 RSS 校验拒绝。主控在 Linux 5.15、Python 3.13.13 下复算及比对退出 0，原十组复算仍与原 JSON 逐字节一致。

```bash
p13_l_resource=$(mktemp)
.venv/bin/python -B docs/round2/performance/p13-05-resource-coverage.py --l-followups \
  docs/round2/performance > "$p13_l_resource"
cmp "$p13_l_resource" docs/round2/performance/p13-05-resource-coverage-l-followups.json
```
