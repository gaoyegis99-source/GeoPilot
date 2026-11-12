# P13-05 修正版矢量 L 档复测

正式批次按点相交、线相交、按面积分配依次测量。原版为 `977ab412d9f6e7558d22a5c431fac0ada90b07be`，修正版为 `a2d2e6874fd7d5d2a239de9d95a927f8505e2ebc`。原生环境使用 Python 3.12.3、QGIS 3.44.15 和 `/dev/sdb1` ext4。每侧冷、暖各 2 次预热和 5 次正式计量。执行命令为 `bash artifacts/test-results/p13-05-vector-l-plan.a2d2e68.C905zE/run-l.sh --run`。

## 点相交原版

原版在 12:25:46–12:51:08 UTC 完成 14 个运行，容器退出 1，日志写入退出 0。14 次均 `FAILED/NOT_CHECKED`。空间筛选成功，裁回步骤均在 60 秒达到 `PROCESS_TIMEOUT`，子进程退出 -15。主控和 B 独立核对 14 个运行 ID，均与 P13-03 的 140 个历史 L 样本不同。输入 manifest 的 SHA-256 `ada61ccadb58accc19039b9f30c24db3d25ac0dc11d09987148105e10dac9c24` 与 P13-03 原始 L 样本一致。 [独立复算数据](p13-05-vector-l-point-base.json)

| 正式计量组 | 失败数 | 端到端中位数（秒） | 端到端 P95（秒） | 裁回步骤中位数（秒） | RSS partial |
| --- | ---: | ---: | ---: | ---: | ---: |
| 冷态 | 5 | 96.612 | 97.374 | 60.667 | 3 / 5 |
| 暖态 | 5 | 96.772 | 97.139 | 60.689 | 3 / 5 |

墙钟分位按失败样本计算。原版严格自比较保留失败分布，成功分位留空。原始分片位于 `/mnt/datab/geopilot-p13-05-vector-l.a2d2e68.C905zE/t13-points-intersects/base/attempt.zz4iJj/`，原始 JSONL、输入清单、配置回执和自比较结果均已收入完整配对归档。

## 点相交修正版

修正版在 12:51:10–13:03:42 UTC 完成 14 次运行，均 `SUCCEEDED/PASSED`，容器和日志写入均退出 0。每次筛选及裁回各保留 65,536 个点，两步均退出 0，两项成果和检查报告与独立参考一致。主控和 B 核对了 28 个不同运行 ID，两侧回执一致，原始归档重放与实时配对逐字节相同。 [严格配对](p13-05-vector-l-point-comparison.json) · [完整原始证据和独立复算](p13-05-vector-l-point-evidence.tar.gz) · [复算日志](p13-05-vector-l-point-verification.log)

| 修正版正式组 | 通过数 | 端到端中位数（秒） | 端到端 P95（秒） | 裁回中位数（秒） | RSS partial |
| --- | ---: | ---: | ---: | ---: | ---: |
| 冷态 | 5 | 38.719 | 39.384 | 14.640 | 2 / 5 |
| 暖态 | 5 | 38.017 | 40.887 | 14.435 | 3 / 5 |

两侧均成功完成筛选步骤，冷态中位数由 30.313 秒降至 14.686 秒，暖态由 30.242 秒降至 14.223 秒。原版裁回及端到端无成功样本，成功差值留空。修正版原始峰值 RSS 和临时文件观测随归档保留。完整 L 点相交配对已核验，线相交随后启动。 [栅格队列](p13-05-raster-s.md) · [已入库的复算工具](p13-05-runner-reproducibility.md)

```bash
p13_unpack=$(mktemp -d)
tar -xzf docs/round2/performance/p13-05-vector-l-point-evidence.tar.gz -C "$p13_unpack"
(cd "$p13_unpack" && sha256sum -c SHA256SUMS)
p13_point="$p13_unpack/t13-points-intersects"
.venv/bin/python -B scripts/bench/run_memory_profile.py --kind vector --require-runtime-configuration \
  --before "$p13_point/base/$(cat "$p13_point/base/COMPLETED")/evidence" \
  --after "$p13_point/candidate/$(cat "$p13_point/candidate/COMPLETED")/evidence"
```

## 线相交修正版冷态

修正版冷态 2 次预热及 5 次正式计量均通过。每次筛选及裁回各保留 131,072 条线要素，总长度 4,030,464 米，符合独立参考。5 次正式计量的端到端中位数为 50.966 秒、P95 为 53.578 秒，筛选及裁回步骤中位数分别为 17.080、19.199 秒。端到端 RSS 有 2 个 partial 样本。7 个运行 ID 与点相交的 28 个运行和 P13-03 的 140 个历史样本无交集。 [冷态单侧原始样本复算](p13-05-vector-l-line-cold.json)

## 线相交修正版完成

修正版在 13:03:43–13:20:16 UTC 完成冷暖两组，容器和日志写入均退出 0。14 次全部 `SUCCEEDED/PASSED`，每次筛选、裁回各 131,072 条线要素，总长度 4,030,464 米，两步骤均退出 0。暖态 5 次正式计量的端到端中位数为 50.333 秒、P95 为 57.004 秒，筛选和裁回步骤中位数分别为 17.261、19.680 秒。暖态端到端 RSS 有 3 个 partial 样本。 [完整候选样本复算](p13-05-vector-l-line-candidate.json)

主控与 B 独立核对了候选侧配置回执、14 个独立运行 ID 和冷态封存记录。便携副本的严格自比较离线复算与实时输出逐字节相同，退出 0。

## 线相交原版冷态

原版冷态两次预热与五次正式计量全部在 `apply_spatial_relation` 触发 60 秒 `PROCESS_TIMEOUT`，进程退出 -15，裁回步骤未启动。正式计量截至失败的端到端中位数为 66.925 秒、P95 为 67.190 秒。RSS 有 1 个 partial 样本，临时峰值的五次采样均完整。主控与 B 独立核对了七个运行 ID，与点相交、线相交修正版及历史 L 样本均无交集。 [原版冷态复算](p13-05-vector-l-line-base-cold.json)

## 线相交完整配对

原版在 13:20:18–13:38:48 UTC 完成 14 次运行，容器退出 1，日志写入退出 0。暖态全部在相同的空间关系判定步骤超时，正式失败中位数为 66.991 秒、P95 为 67.315 秒，端到端 RSS 有 3 个 partial 样本。

两侧严格比较匹配同一输入、配置回执与运行环境，28 个运行 ID 与点相交及历史 L 档记录无交集。原版无成功样本，冷热组的成功耗时、RSS 和临时空间差值均留空。主控与 B 分别复算两侧原始 JSONL，归档解包重放与实时配对逐字节一致，退出 0。按面积分配原版于 13:38:49 UTC 启动。 [严格配对](p13-05-vector-l-line-comparison.json) · [完整原始证据和独立复算](p13-05-vector-l-line-evidence.tar.gz) · [复算日志](p13-05-vector-l-line-verification.log)

## 按面积分配原版冷态

原版冷态两次预热与五次正式计量均在 `apply_spatial_relation` 触发 60 秒超时，进程退出 -15，裁回和按区汇总均未启动。正式计量截至失败的端到端中位数为 65.331 秒、P95 为 65.620 秒，RSS 有 3 个 partial 样本。主控与 B 核对了 7 个运行 ID，与本轮点、线相交的 56 条记录及历史 L 档 140 条记录无交集。P13-03 同负载的 14 次运行使用相同输入，失败步骤一致。 [原版冷态复算](p13-05-vector-l-area-base-cold.json)

## 按面积分配原版完成

原版在 13:38:49–13:56:32 UTC 完成 14 次运行，容器退出 1，日志写入退出 0。暖态也均在空间关系判定步骤超时，正式失败中位数为 65.423 秒、P95 为 65.581 秒。暖态端到端 RSS 五个样本均有部分采样，临时峰值五个样本均完整。

主控与 B 核对了原版配置回执、14 个独立运行 ID、冷态封存记录和下游未启动状态。原版便携副本的严格自比较复算与实时输出逐字节相同，退出 0。修正版于 13:56:34 UTC 启动。 [完整原版样本复算](p13-05-vector-l-area-base.json)

## 按面积分配冻结配对

冻结修正版在 13:56:34–14:16:17 UTC 完成 14 次运行，容器退出 1，日志写入退出 0。每次空间关系判定成功筛选 131,072 条目标，随后在 `clip_back_to_analysis_extent` 触发 60 秒 `PROCESS_TIMEOUT`，子进程退出 -15，按区汇总未启动。B 核对源码与输入配方，零命中及多命中的目标仍走完整范围的叠加处理。

| 版本和正式计量组 | 失败数 | 端到端失败中位数（秒） | 端到端失败 P95（秒） | 失败步骤 |
| --- | ---: | ---: | ---: | --- |
| 原版冷态 | 5 | 65.331 | 65.620 | 空间关系判定 |
| 原版暖态 | 5 | 65.423 | 65.581 | 空间关系判定 |
| 冻结修正版冷态 | 5 | 74.136 | 75.869 | 裁回 |
| 冻结修正版暖态 | 5 | 73.797 | 74.941 | 裁回 |

冻结修正版空间关系判定的成功中位数为冷态 7.371 秒、暖态 7.297 秒。严格比较匹配同一输入、环境和配置回执，配对及两份自比较在便携证据上逐字节重放。主控和 B 核对 28 个运行 ID，与本轮 56 个点、线相交及 140 个历史 L 样本无交集。两侧均无端到端成功样本，成功耗时和资源差值留空。冻结配对完成后，遥感 S 重测于 14:16:19 UTC 开始。 [严格配对](p13-05-vector-l-area-comparison.json) · [原始样本及复算](p13-05-vector-l-area-evidence.tar.gz) · [复算日志](p13-05-vector-l-area-verification.log)

## 按面积分配局部裁回候选

隔离提交 `fff225f223568921ef2ac56f2b76ee9c4f8cbd29` 基于冻结修正版 `a2d2e68`，只改动裁回工具和专项测试。已声明 XY、零精度的面要素在整坐标轴对齐矩形范围中，零包围盒命中直接得到空结果。对恰好命中两个分离矩形且各有正面积相交的矩形目标，按原范围组件顺序构造局部 MultiPolygon。边界接触、孔、三个及以上命中、分数坐标范围和未声明 XY 的输入继续使用既有处理。

B 在主机 Shapely 2.1.2、GEOS 3.13.1 上复跑 54 项裁回及方法回归，退出 0。真实 GeoPackage 测试比较原目标和裁回结果的逐字节 WKB、行序、重复源 ID、属性类型和值、CRS 及边界接触。另有 5,120 个组件顺序、环顺序、边界和坐标平移对照通过。在 256 个瓦片、2,048 个目标的小窗上，五次裁回计算中位数由 102.791 毫秒降至 31.776 毫秒，两侧逐轮 WKB 一致。16 瓦片小窗从 2.653 毫秒增至 2.715 毫秒。这些计时只覆盖小窗裁回计算，L 档的全方法正确性、耗时和资源仍待正式复测。 [命令、候选补丁、原始计时及 JUnit](p13-05-t14-local-clip-evidence.tar.gz)

主控在 `b989cca` 的独立工作树依次重放 `e874a0f`、`a2d2e68`、分区重投影暂存修补 `b126785` 和局部裁回候选，得到组合预演 `d98d22b08cf275a2c52fb905c62fe2e16631a600`。裁回、方法、源身份、受管写入、暂存与取消共 87 项测试通过，0 失败、0 跳过，pytest 退出 0。解包 SHA-256、实际命令和两份 JUnit 均已独立核对。 [分区重投影和暂存复验](p13-05-staging-resources.md#分区重投影与终态回执)

A 按候选精确提交和原五个运行器覆盖文件重建源码，树清单 SHA-256 为 `05258667966a75b5695ca10e9835daf9bb36d050fd8254fdfe95eabef18930e7`。旧版和新版预演均退出 0，Docker 调用为 0。正式候选沿用每个缓存组 2 次预热、5 次计量和 600 秒等待上限，使用新结果根。原版 14 条样本按封存校验和严格自比较只读复用。候选启动已排在遥感 S、DEM M、DEM L 和遥感 L 四组后，取得共用测量锁后开始。正式命令及预演原始输出在候选证据归档的 `launcher/`。 [启动器、源码清单及预演回执](p13-05-t14-local-clip-evidence.tar.gz)

## T14 局部裁回候选冷态

候选于 16:56:12 UTC 启动。原生 Python 3.12.3、QGIS 3.44.15 与 `/dev/sdb1` ext4 沿用冻结配对环境。正式源码清单与预演摘要一致，输入清单与原版及冻结修正版逐字节相同。两次冷预热和五次正式计量均为 `SUCCEEDED/PASSED`，三步退出码均为 0。每轮筛选 131,072 条、裁回 114,688 条，按 32,768 个区汇总 98,304 个对象，产生 147,456 条分配明细，总面积 36,044,800 m²。 [冷态原始记录、环境和独立复算](p13-05-t14-local-clip-cold-evidence.tar.gz)

| 五次正式计量 | 中位数，秒 | P95，秒 |
| --- | ---: | ---: |
| 端到端 | 68.182 | 69.035 |
| 空间关系筛选 | 7.277 | 7.533 |
| 裁回 | 15.984 | 16.227 |
| 按区汇总 | 31.786 | 32.389 |

整轮 RSS 两次完整、三次部分采样，裁回 RSS 四次完整、一次部分采样。五次临时空间均有完整采样，峰值中位数／P95 为 309,053,477／319,399,909 字节。原版冷组五次在筛选步超时，冻结修正版冷组五次在裁回步超时，两侧成功分位及成功耗时差值留空。正式候选的暖组已接续。

基准在冷态 worker 退出后清理临时工作区，保留逐轮检查、独立几何验算与四个登记指纹。主控从封存输入的逐块参考直接重建 7,094,321 字节的分配 CSV 和 8,602,009 字节的分区 JSON，计算出的内容文件组指纹与七轮登记一致。同一方法对 M 档封存配对的两侧 28 轮也全部匹配。篡改隔离副本中一个登记指纹后，复算拒绝该副本，退出 1。B 从原始记录独立核对几何验算、三步日志与资源采样，复算退出 0。完整单侧回执及前后配对见下节。 [M 档正式复测](p13-05-vector-m-revised.md)

```bash
p13_t14_cold=$(mktemp -d)
tar -xzf docs/round2/performance/p13-05-t14-local-clip-cold-evidence.tar.gz -C "$p13_t14_cold"
(cd "$p13_t14_cold" && sha256sum -c SHA256SUMS)
PYTHONPATH="$p13_t14_cold/source" .venv/bin/python -B \
  "$p13_t14_cold/verify_t14_cold.py" "$p13_t14_cold" > "$p13_t14_cold/replay.json"
cmp "$p13_t14_cold/replay.json" "$p13_t14_cold/cold-replayed.json"
PYTHONPATH="$p13_t14_cold/source" .venv/bin/python -B \
  "$p13_t14_cold/check-m-reference.py" docs/round2/performance/p13-05-vector-m-revised-evidence.tar.gz \
  > "$p13_t14_cold/m-replayed.json"
cmp "$p13_t14_cold/m-replayed.json" "$p13_t14_cold/m-reference.json"
```

## T14 局部裁回候选完整配对

候选于 17:18:04 UTC 结束。暖态 2 次预热和 5 次正式计量全部通过，冷暖共 14 轮为 `SUCCEEDED/PASSED`，三步均退出 0。候选容器和日志写入退出码均为 0。配置回执一致，严格比较器返回 `MATCHED_EVIDENCE`。两侧 28 个运行 ID 互异，14 个新 ID 与此前 224 个 L 档运行互异，冷态前缀与已封存证据逐字节一致。 [完整配对、原始逐轮记录和两份复算](p13-05-t14-local-clip-pair-evidence.tar.gz)

| 暖态五次正式计量 | 中位数，秒 | P95，秒 |
| --- | ---: | ---: |
| 端到端 | 67.738 | 69.365 |
| 空间关系筛选 | 7.166 | 7.347 |
| 裁回 | 15.822 | 18.090 |
| 按区汇总 | 31.659 | 33.387 |

暖态整轮 RSS 一次完整、四次部分采样，临时空间四次完整、一次部分采样。整轮 RSS 中位数／P95 为 517,513,216／520,974,336 字节，临时空间为 304,252,965／319,543,269 字节。正式原版冷暖 14 轮均在首步超时，冻结修正版冷暖 14 轮均在裁回步超时。正式原版和局部裁回候选的成功样本差值留空。

主控从独立参考重建的两类文本成果与冷暖 14 轮登记指纹均一致，篡改暖态登记指纹的副本被拒绝，退出 1。B 独立核对全部几何验算、四个成果角色登记和逐步日志。原版自比较、候选自比较和两侧配对离线重放逐字节一致，退出 0。DEM M 原版重试于 17:18:08 UTC 接续，DEM L 容量探针仍排在重试之后。

```bash
p13_t14_pair=$(mktemp -d)
mkdir "$p13_t14_pair"/{previous,cold,pair}
tar -xzf docs/round2/performance/p13-05-vector-l-area-evidence.tar.gz -C "$p13_t14_pair/previous"
tar -xzf docs/round2/performance/p13-05-t14-local-clip-cold-evidence.tar.gz -C "$p13_t14_pair/cold"
tar -xzf docs/round2/performance/p13-05-t14-local-clip-pair-evidence.tar.gz -C "$p13_t14_pair/pair"
for group in previous cold pair; do
  (cd "$p13_t14_pair/$group" && sha256sum -c SHA256SUMS) || exit 1
done
.venv/bin/python -B "$p13_t14_pair/pair/verify-complete.py" \
  "$p13_t14_pair/pair" "$p13_t14_pair/previous" "$p13_t14_pair/cold" > "$p13_t14_pair/replay.json"
cmp "$p13_t14_pair/replay.json" "$p13_t14_pair/pair/replayed.json"
```

复算另需此前归档的原版和冻结修正版，以及参考重建脚本。 [原版和冻结修正版归档](p13-05-vector-l-area-evidence.tar.gz) · [冷态与参考重建归档](p13-05-t14-local-clip-cold-evidence.tar.gz)
