# P13-05 栅格 M/L 正式复测

DEM M 在遥感 S 重测结束后于 14:23:57 UTC 启动。原版为 `977ab412d9f6e7558d22a5c431fac0ada90b07be`，修正版为 `a2d2e6874fd7d5d2a239de9d95a927f8505e2ebc`。两侧沿用固定镜像、Python 3.12.3、QGIS 3.44.15 和 `/dev/sda2` ext4。每侧冷、暖各 2 次预热和 5 次正式计量。 [输入清单和执行配方](p13-05-launch-recipes.md) · [遥感 S 配对](p13-05-raster-s.md#遥感-s-重测)

## DEM M 修正版

修正版完成 14 轮，容器退出 1，自比较退出 0。冷态两次预热和五次正式计量均通过。暖态两次预热及一次正式计量因状态查询错误记为失败，三次分别记录 3、1、2 次 HTTP 500 `STORAGE_FAILED`。三次最终均 `SUCCEEDED/PASSED`，七步退出码均为 0。暖态失败的正式样本原始耗时为 64.025 秒，单条失败记录的分位留空。 [原始记录、终态 SQLite 审计及独立复算](p13-05-dem-m-candidate-evidence.tar.gz)

| 正式计量组 | 通过 | 失败 | 成功端到端中位数／P95（秒） | 成功面积步骤中位数（秒） | 成功整轮 RSS partial |
| --- | ---: | ---: | ---: | ---: | ---: |
| 冷态 | 5 | 0 | 46.168／48.919 | 37.312 | 5 / 5 |
| 暖态 | 4 | 1 | 49.798／52.816 | 37.948 | 4 / 4 |

主控从 8 份终态 SQLite 副本核对了 14 个运行、98 个步骤、正式成果登记和检查报告，108 个源文件前后摘要一致。B 复算 14 条原始记录，核对输入、配置回执及七步参数，并确认新运行 ID 与此前核对的 159 个 ID 无交集。14 轮类别 7 均为 4,183,812 像元，NoData 为 2,304 像元，地形及分类最大像元误差均为 0。观测等积面积 418,714,664.9050148 m²，与独立参考相差约 0.000781 m²。

修正版单侧归档可核对输入清单、报告回执、完整冷暖计划和原始冷态前缀，并逐字节重放严格自比较。

```bash
p13_dem_replay=$(mktemp -d)
tar -xzf docs/round2/performance/p13-05-dem-m-candidate-evidence.tar.gz -C "$p13_dem_replay"
(cd "$p13_dem_replay" && sha256sum -c SHA256SUMS)
.venv/bin/python -B "$p13_dem_replay/proof/replay-side.py" "$p13_dem_replay"
```

## DEM M 原版冷态

原版冷态两次预热及五次正式计量均在 `calculate_area_with_equal_area_projection` 触发 60 秒 `PROCESS_TIMEOUT`，子进程退出 -15，终态为 `FAILED/NOT_CHECKED`。五次正式计量截至失败的端到端中位数为 65.966 秒、P95 为 67.794 秒。第二次预热另记录一次 HTTP 500，最终持久错误仍为面积计算超时。 [原始前缀复算](p13-05-dem-m-base-cold.json)

## DEM M 原版暖态中断

原版容器退出 1，启动器退出 2。原版共记录 11 轮，其中冷态 7 轮及暖态前三轮均在等积面积步触发 60 秒超时。暖态第四轮是第二次正式计量，连续三次状态查询和随后一次取消请求均返回 HTTP 500。基准停止该组，暖态 worker 退出 -15，剩余三次正式计量缺失。原版缺完成指针与配置回执，强制比较退出 1。暖态两个正式失败观测分别为 70.206 和 35.636 秒，分位留空。B 核对了两侧 25 个已观测运行 ID，与此前核对的 159 个 ID 无交集。 [原始中断记录、回执反例和复算](p13-05-dem-m-incomplete-evidence.tar.gz)

D 从暖态第四轮的 SQLite 和回滚日志副本核对。最后一次 HTTP 200 为 `RUNNING/NOT_CHECKED`，当前步骤为读取扩展范围 DEM。数据库中仍为 `RUNNING/NOT_CHECKED`，已推进到阈值分类。前五步持久化成功。第六步磁盘结果和过程日志均显示退出 0，SQLite 仍记录运行中。第七步等积面积计算为 `NOT_STARTED`，无面积步骤请求或成果。取消标记未提交，无持久错误码。37 个源文件前后摘要相同。主控从解包副本重放 SQLite 查询，持久状态和磁盘步骤结果一致，退出 0。

DEM L 于 14:53:29 UTC 开始。A 验证了在同一结果根复用封存的修正版、保留旧原版中断分片并只启动新原版。只读预演和小夹具退出 0。原版重试已排在当前栅格和 T14 候选之后，沿用冷热各 2 次预热、5 次计量及同一测量锁。新原版完成并取得配置回执后才生成严格配对。启动命令、夹具和排队回执随中断归档保存。

## DEM M 原版重试冷态

原版重试于 17:18:08 UTC 启动。冷态两次预热、五次正式计量均完成前六步，随后在等积面积步达到 60 秒限时。全部持久终态为 `FAILED/NOT_CHECKED`，子进程退出 -15，原始冷态记录无状态查询失败。五次正式失败样本端到端中位数／P95 为 65.957／66.331 秒，面积步骤为 60.048／60.049 秒，整轮 RSS 五次部分采样，临时空间五次完整采样。

主控复验七份分类 GeoTIFF 的指纹、网格、类别 7 的 4,183,812 个像元和 2,304 个 NoData 像元，均与 P13-04 参考一致。B 独立对照旧原版和候选冷组，三侧 21 份分类 TIFF 字节相同。重试七个运行 ID 与此前 25 个 DEM M ID 无交集。重试冷组停在完整成果验算之前。暖组已接续，完整配对仍待暖态终态。 [冷态原始记录、GeoTIFF 和两份复算](p13-05-dem-m-retry-cold-evidence.tar.gz) · [封存分类参考](p13-04-dem-m-classification-source.json)

## DEM M 原版重试正式配对

重试启动器于 17:34:09 UTC 退出 0，原版容器以 1 报告完整失败组。原版冷暖 14 轮均在等积面积步骤触发 60 秒 `PROCESS_TIMEOUT`，全部持久终态为 `FAILED/NOT_CHECKED`，无状态 GET 错误。复用的修正版冷暖 14 轮均完成计算和独立验算，暖态两次预热和一次正式计量保留基准状态观察失败。配置回执一致，严格配对为 `MATCHED_EVIDENCE`，两侧各 14 个运行 ID 互异。 [正式比较、原始记录、分类和回执](p13-05-dem-m-retry-pair-evidence.tar.gz)

| 正式计量 | 端到端中位数，秒 | P95，秒 | 基准结果 |
| --- | ---: | ---: | --- |
| 重试原版冷态 | 65.957 | 66.331 | 5 次面积超时 |
| 重试原版暖态 | 65.874 | 66.081 | 5 次面积超时 |
| 修正版冷态成功样本 | 46.168 | 48.919 | 5 次通过 |
| 修正版暖态成功样本 | 49.798 | 52.816 | 4 次通过，1 次状态观察失败 |

原版冷态整轮 RSS 五次部分采样，暖态两次完整、三次部分。两侧的成功样本差值留空。主控核验重试冷前缀、14 份分类 TIFF、原始 worker 日志与重复使用的修正版分片。每份分类与 P13-04 封存参考逐字节一致。B 独立验证两侧共 28 份分类、修正版完整成果和正式回执，复算退出 0。原版自比较、候选自比较及正式配对离线重放退出 0，结果逐字节一致。旧暖态 HTTP 500 中断继续保留，重试没有重现该错误。DEM L 整幅面积容量探针于 17:34:13 UTC 接续。

```bash
p13_dem_m=$(mktemp -d)
mkdir "$p13_dem_m"/{cold,pair}
tar -xzf docs/round2/performance/p13-05-dem-m-retry-cold-evidence.tar.gz -C "$p13_dem_m/cold"
tar -xzf docs/round2/performance/p13-05-dem-m-retry-pair-evidence.tar.gz -C "$p13_dem_m/pair"
(cd "$p13_dem_m/cold" && sha256sum -c SHA256SUMS)
(cd "$p13_dem_m/pair" && sha256sum -c SHA256SUMS)
.venv/bin/python -B "$p13_dem_m/pair/proof/replay.py" "$p13_dem_m/pair" dem-m
.venv/bin/python -B "$p13_dem_m/pair/proof/verify-dem-m.py" \
  "$p13_dem_m/pair" "$p13_dem_m/cold" docs/round2/performance/p13-04-dem-m-classification-source.json \
  > "$p13_dem_m/replayed.json"
cmp "$p13_dem_m/replayed.json" "$p13_dem_m/pair/proof/replayed.json"
```

## DEM L 原版冷态

两次预热和五次正式计量全部完成前六步，随后在等积面积步触发 60 秒 `PROCESS_TIMEOUT`，子进程退出 -15，终态为 `FAILED/NOT_CHECKED`。三轮曾出现状态查询错误，随后均读取到超时终态。五次正式计量截至失败的端到端中位数和 P95 为 86.145、98.954 秒，面积步为 60.049、60.051 秒。五次临时空间采样完整，峰值中位数为 1,843,780 字节。整轮 RSS 一次完整、四次部分采样，观察中位数为 249,741,312 字节。B 核对 7 个新运行 ID，与此前核对的 184 个 ID 无交集。 [原始样本、资源记录和两份复算](p13-05-dem-l-base-cold-evidence.tar.gz)

主控按实际步骤请求读取七份阈值分类中间 GeoTIFF，均为 128,239 字节，SHA-256 均为 `7a62aed10a269d83989d10d9f35b12527ef4be1be523e51bd0300649549213e8`，与 P13-04 封存参考逐字节相同。网格为 EPSG:32650 的 8190 × 8190 像元，类别 7 有 67,039,236 个像元，NoData 有 36,864 个像元。选中像元约为 DEM M 的 16 倍。面积结果和正式成果清单均未生成，冻结 runner 的成果验算未启动。解包后重放原始 7 条记录、请求与 TIFF 核对，退出 0。 [封存分类参考](../../../benchmarks/raster/area-fixtures/README.md)

## DEM L 原版完成

暖态七轮也均在第七步等积面积计算超时。原版 14 轮均完成前六步并取得 `FAILED/NOT_CHECKED` 持久终态。共记录 17 次状态查询错误，全部对应运行均取得明确超时终态。容器退出 1，配置回执绑定完整报告，自比较退出 0。主控和 B 核对冷态前缀原始字节一致、14 份中间分类 TIFF 指纹一致，14 个运行 ID 与先前核对的 184 个 ID 无交集。解包重放严格自比较，与原输出逐字节相同。 [完整原始证据和独立复算](p13-05-dem-l-base-evidence.tar.gz)

| 正式计量组 | 失败数 | 失败端到端中位数／P95（秒） | 面积步骤中位数／P95（秒） | 整轮 RSS partial | 临时峰值中位数（字节） |
| --- | ---: | ---: | ---: | ---: | ---: |
| 冷态 | 5 | 86.145／98.954 | 60.049／60.051 | 4 / 5 | 1,843,780 |
| 暖态 | 5 | 95.948／114.665 | 60.050／60.909 | 4 / 5 | 1,778,244 |

冻结修正版随后启动。

```bash
p13_dem_l=$(mktemp -d)
tar -xzf docs/round2/performance/p13-05-dem-l-base-evidence.tar.gz -C "$p13_dem_l"
(cd "$p13_dem_l" && sha256sum -c SHA256SUMS)
.venv/bin/python -B "$p13_dem_l/proof/replay-original.py" "$p13_dem_l"
```

## DEM L 冻结修正版冷态

两次预热和五次正式计量均完成前六步，等积面积步触发 60 秒 `PROCESS_TIMEOUT`，子进程退出 -15。七次状态查询错误后仍读取到明确超时终态。两侧冷组的输入清单和归一化步骤请求一致，14 份冷态分类中间 TIFF 与封存参考逐字节相同。完整方法的成果正确性验算未启动。修正版正式失败端到端中位数和 P95 为 98.021、105.280 秒，面积步骤为 60.050、60.507 秒。五次整轮 RSS 均为部分采样，临时峰值均有完整采样，峰值中位数为 1,843,780 字节。两侧冷组均无成功样本。七个新运行 ID 与之前核对的 198 个 ID 无交集。 [冷组原始记录和两次复算](p13-05-dem-l-candidate-cold-evidence.tar.gz)

## DEM L 正式配对

原版与冻结修正版各完成 14 轮，容器均退出 1。28 轮均完成前六步，在第七步等积面积计算触发 60 秒 `PROCESS_TIMEOUT`，子进程退出 -15，取得 `FAILED/NOT_CHECKED` 终态。原版和修正版分别记录 17、28 次状态查询 HTTP 500，随后均读到终态。28 份阈值分类 TIFF 都与封存参考逐字节相同。两侧均无正式成果清单，完整成果正确性验证未启动。 [两侧原始记录、回执、TIFF 和独立审计](p13-05-dem-l-pair-evidence.tar.gz)

| 正式计量组 | 原版失败端到端中位数／P95（秒） | 修正版失败端到端中位数／P95（秒） | 原版整轮 RSS partial | 修正版整轮 RSS partial |
| --- | ---: | ---: | ---: | ---: |
| 冷态 | 86.145／98.954 | 98.021／105.280 | 4 / 5 | 5 / 5 |
| 暖态 | 95.948／114.665 | 134.058／156.479 | 4 / 5 | 5 / 5 |

每组各 5 个正式失败样本。暖态面积步骤中位数／P95 为原版 60.050／60.909 秒、修正版 60.050／63.116 秒。两侧配置回执一致，各自绑定完整报告。主控解包复算两侧严格自比较和正式配对，与原输出逐字节相同，比较器退出 0，返回 `MATCHED_EVIDENCE`。B 独立核对两侧冷态前缀、归一化步骤参数及 28 个新运行 ID，与此前的 184 个 ID 无交集。两侧均有 0 个成功样本，成功样本差值为空。遥感 L 原版于 15:48:59 UTC 接续启动。 [严格配对结果](p13-05-dem-l-comparison.json)

对每个正式样本，以 `execute_steps_wall_ns` 减去各步骤适配器记录的墙钟耗时之和，取组内中位数。冷态原版／修正版为 0.111／11.504 秒，暖态为 8.538／26.517 秒。面积子进程的已采样 CPU 时间中位数四组均在 59.920 至 59.950 秒间。未纳入步骤适配器计时的区段包含请求编译、前序成果读取及步骤请求与结果的 SQLite 持久化。现有遥测无法确定各项各占多少。逐次输入与复算脚本随配对归档。 [SQLite 事务诊断](p13-07.md#状态查询与步骤提交的锁边界)

```bash
p13_dem_l_pair=$(mktemp -d)
tar -xzf docs/round2/performance/p13-05-dem-l-pair-evidence.tar.gz -C "$p13_dem_l_pair"
(cd "$p13_dem_l_pair" && sha256sum -c SHA256SUMS)
.venv/bin/python -B "$p13_dem_l_pair/proof/verify-dem-l-pair.py" "$p13_dem_l_pair"
```

## 遥感 L 原版冷态

原版两次预热和五次正式计量均完成前七步。第八步 `calculate_change` 在生产 60 秒限制触发 `PROCESS_TIMEOUT`，子进程退出 -15，七轮均取得 `FAILED/NOT_CHECKED`。共记录 10 次状态查询 HTTP 500。未生成正式成果清单或该步骤三个目标文件，完整成果正确性验证未启动。 [原始冷态前缀、步骤请求及结果](p13-05-remote-l-base-cold-evidence.tar.gz)

五次正式失败样本的端到端中位数／P95 为 126.105／129.116 秒，变化计算步骤为 60.050／60.054 秒。整轮 RSS 均为部分采样，变化计算步 RSS 三次完整、两次部分采样。变化计算进程已采样 CPU 时间为 59.940 至 59.970 秒。五次临时空间峰值均有完整采样，中位数为 11,181,073 字节。

每轮在变化计算步留下 311,039 字节的差值 TIFF 和 44,420 字节的分类 TIFF。两类文件各自在七轮间逐字节一致。分类 TIFF 与 P13-04 封存参考一致，在 EPSG:32632 的 3072 × 3072 网格中，类别 1、2、3 各 1,572,864 个像元，掩膜为 4,718,592 个像元。主控和 B 分别复算，均退出 0。B 复核七个新运行 ID 与此前核对的 212 个 ID 无交集。归档同时保存输入清单、原始请求、磁盘步骤结果及 P13-04 同负载原始记录。 [P13-04 分类来源](p13-04-remote-l-classification-source.json)

```bash
p13_remote_l=$(mktemp -d)
tar -xzf docs/round2/performance/p13-05-remote-l-base-cold-evidence.tar.gz -C "$p13_remote_l"
(cd "$p13_remote_l" && sha256sum -c SHA256SUMS)
env OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 GDAL_NUM_THREADS=1 PROJ_NETWORK=OFF \
  .venv/bin/python -B "$p13_remote_l/verify-remote-cold.py" "$p13_remote_l" > "$p13_remote_l/replay.json"
cmp "$p13_remote_l/replay.json" "$p13_remote_l/full-cold.json"
```

## 遥感 L 原版完成

原版完成 14 轮，容器退出 1，全部在第八步 `calculate_change` 触发 60 秒超时。暖态三次状态查询 HTTP 500 后均取得超时终态，两组合计 13 次。14 份分类 TIFF 均与 P13-04 参考逐字节一致，14 份差值 TIFF 也各自一致。每轮保留两份暂存 TIFF 共 355,459 字节，未生成正式成果，完整成果验算未启动。冷组 JSONL 前缀与原封存逐字节相同。主控及 B 独立复算均退出 0，B 确认 14 个运行 ID 与先前核对的 212 个 ID 无交集。 [完整原始记录、回执、TIFF 和独立审计](p13-05-remote-l-base-evidence.tar.gz)

| 正式失败计量组 | 端到端中位数／P95，秒 | 变化计算中位数／P95，秒 | 整轮 RSS 部分采样 | 临时峰值中位数，字节 |
| --- | ---: | ---: | ---: | ---: |
| 冷态 | 126.105／129.116 | 60.050／60.054 | 5 / 5 | 11,181,073 |
| 暖态 | 122.782／130.107 | 60.047／60.048 | 5 / 5 | 11,181,073 |

两组正式临时峰值均有完整采样。暖态变化计算步 RSS 为两次完整、三次部分采样。配置回执绑定完整报告。严格自比较离线重放与现场输出逐字节一致，退出 0，成功样本差值为空。冻结修正版于 16:22:03 UTC 接续启动。

```bash
p13_remote_base=$(mktemp -d)
tar -xzf docs/round2/performance/p13-05-remote-l-base-evidence.tar.gz -C "$p13_remote_base"
(cd "$p13_remote_base" && sha256sum -c SHA256SUMS)
.venv/bin/python -B "$p13_remote_base/proof/verify-remote-original.py" "$p13_remote_base" > "$p13_remote_base/replay.json"
cmp "$p13_remote_base/replay.json" "$p13_remote_base/proof/replayed.json"
```

## 遥感 L 冻结修正版首轮

首个冷态预热八步退出码均为 0，第八步变化计算用时 51.239 秒。生产运行和检查终态为 `SUCCEEDED/PASSED`。三次状态查询 HTTP 500 触发基准 `StatusObservationFailure`，该轮仍按基准记为失败。主控从该终态运行复制并复算 10 个正式成果文件组的登记指纹，合计 5,054,589 字节。变化与分类 TIFF 分别与原版暂存文件逐字节一致。三个分类各 1,572,864 像元，EPSG:6933 面积合计 4,249,836,701.624876 m²，与封存独立参考的最大单类绝对差约 0.007379 m²。归档含原始记录、检查报告及实际成果。 [首个预热的完整成果复算](p13-05-remote-l-candidate-first-evidence.tar.gz)

```bash
p13_remote_first=$(mktemp -d)
tar -xzf docs/round2/performance/p13-05-remote-l-candidate-first-evidence.tar.gz -C "$p13_remote_first"
(cd "$p13_remote_first" && sha256sum -c SHA256SUMS)
PYTHONPATH="$p13_remote_first/proof" .venv/bin/python -B \
  "$p13_remote_first/verify-first.py" "$p13_remote_first" > "$p13_remote_first/replay.json"
cmp "$p13_remote_first/replay.json" "$p13_remote_first/verified.json"
```

## 遥感 L 冻结修正版冷态

冷态两次预热和五次正式计量均完成八步，持久终态为 `SUCCEEDED/PASSED`。7 轮的 70 个正式成果文件组均重新计算指纹并与登记一致，每个角色在各轮之间的指纹相同。变化及分类 TIFF 与原版暂存文件分别逐字节一致。三个分类各 1,572,864 像元，NoData 为 4,718,592 像元，面积合计 4,249,836,701.624876 m²，最大单类面积差约 0.007379 m²。独立像元验算记录的最大误差均为 0。变化计算目录逐轮只保留三个声明产物。 [原始记录、70 份成果文件组与独立复算](p13-05-remote-l-candidate-cold-evidence.tar.gz)

两次预热及三次正式计量有状态观察失败，共记录 9 次 HTTP 500。五次正式计量中，两次基准通过，端到端原值为 121.180 秒和 114.131 秒，成功分位留空。三次观察失败的端到端中位数／P95 为 133.675／133.752 秒。五次正式计量的变化计算均成功，中位数／P95 为 51.175／51.409 秒。整轮 RSS 均部分采样，变化计算步骤的 RSS 和临时峰值均完整采样，临时峰值中位数为 11,181,073 字节。

主控及 B 独立复算均退出 0，B 核对 7 个新 ID 与此前的 226 个运行 ID 无交集。主控向隔离副本中的面积 JSON 追加空行，登记指纹核验拒绝该副本，退出 1。原始冷态前缀已封存。

```bash
p13_remote_candidate_cold=$(mktemp -d)
tar -xzf docs/round2/performance/p13-05-remote-l-candidate-cold-evidence.tar.gz -C "$p13_remote_candidate_cold"
(cd "$p13_remote_candidate_cold" && sha256sum -c SHA256SUMS)
PYTHONPATH="$p13_remote_candidate_cold/proof" .venv/bin/python -B \
  "$p13_remote_candidate_cold/verify-cold.py" "$p13_remote_candidate_cold" > "$p13_remote_candidate_cold/replayed.json"
cmp "$p13_remote_candidate_cold/replayed.json" "$p13_remote_candidate_cold/cold.json"
```

## 遥感 L 正式配对

两侧各完成 14 轮，容器均退出 1。原版 14 轮均在变化计算步超时。冻结修正版 14 轮均为 `SUCCEEDED/PASSED`，八步退出码全为 0，完成四项检查与独立成果验算。修正版 140 个正式成果文件组全部重算通过，每个角色的指纹跨冷热保持一致。变化和分类 TIFF 分别与原版的暂存 TIFF 逐字节相同，等积面积合计 4,249,836,701.624876 m²。原版每轮留下两份共 355,459 字节的暂存 TIFF，修正版的变化计算目录只保留三个声明产物。 [完整原始记录、成果、回执及独立审计](p13-05-remote-l-pair-evidence.tar.gz)

| 版本和正式计量组 | 基准通过／失败 | 成功端到端中位数／P95，秒 | 失败端到端中位数／P95，秒 | 变化计算中位数／P95，秒 |
| --- | ---: | ---: | ---: | ---: |
| 原版冷态 | 0／5 | — | 126.105／129.116 | 60.050／60.054 |
| 原版暖态 | 0／5 | — | 122.782／130.107 | 60.047／60.048 |
| 修正版冷态 | 2／3 | — | 133.675／133.752 | 51.175／51.409 |
| 修正版暖态 | 3／2 | 122.828／132.368 | — | 51.302／51.743 |

变化计算分位使用每组五条正式记录。原版均触发 60 秒时限，修正版均成功返回。修正版冷态两条成功样本、暖态两条观察失败样本不足三条，分位留空。修正版五次正式失败和两次冷态预热失败均为状态观察失败。两侧各有 13 次状态 GET HTTP 500。四组整轮 RSS 均为部分采样，临时峰值均有完整采样。基准成功样本差值为空。

两侧配置回执一致，并各自绑定完整报告。主控解包重放两侧严格自比较及正式配对，与原输出逐字节一致，退出 0，返回 `MATCHED_EVIDENCE`。B 核对 28 个不同运行 ID 与此前 212 个 ID 无交集。现行比较器从同一封存记录识别全部五次正式观察失败，其他比较字段一致。配对于 16:56:07 UTC 完成，T14 局部裁回候选接续启动。 [冻结严格配对 JSON](p13-05-remote-l-comparison.json) · [T14 候选](p13-05-vector-l.md#按面积分配局部裁回候选)

```bash
p13_remote_pair=$(mktemp -d)
tar -xzf docs/round2/performance/p13-05-remote-l-pair-evidence.tar.gz -C "$p13_remote_pair"
(cd "$p13_remote_pair" && sha256sum -c SHA256SUMS)
PYTHONPATH="$p13_remote_pair/proof" .venv/bin/python -B \
  "$p13_remote_pair/proof/verify-remote-pair.py" "$p13_remote_pair" > "$p13_remote_pair/replayed.json"
cmp "$p13_remote_pair/replayed.json" "$p13_remote_pair/proof/verified.json"
```

## 相邻批次的投影复用预演

隔离候选 `b49025453794331f0a42cb9390a7ed0708c0e7d7` 基于冻结修正版 `a2d2e68`。上一批恰好在 8 段收敛时，当前批与上一批具有相同布局、仿射变换、投影器及列原点，且原点向下移动一行，才复用共同顶点的投影坐标。细化至 16 段起沿用批内复用。三个真实窗口及剪切、列移、退化、非有限坐标和切缝反例的批次顺序、各级坐标、逐格面积、累加或错误均与冻结修正版一致。 [源码补丁、命令、逐次记录与 JUnit](p13-05-cross-batch-area-evidence.tar.gz)

| 256 × 256 分类窗口 | 投影点数，冻结修正版／候选 | CPU 中位数，毫秒 | 墙钟中位数，毫秒 | 进程峰值 RSS 中位数变化 |
| --- | ---: | ---: | ---: | ---: |
| DEM M | 1,510,168／1,004,634 | 527.280／398.827 | 527.504／399.056 | +0.39 MiB |
| DEM L | 1,510,168／1,004,634 | 529.722／401.099 | 530.112／401.430 | +0.40 MiB |
| 遥感 L | 722,787／722,787 | 397.298／396.710 | 397.589／396.926 | −0.16 MiB |

每侧各运行五个独立子进程，交替执行，Python 3.13.13、NumPy 2.4.4、PROJ 9.5.1。两份 DEM 小窗 CPU 中位数下降约 24%，遥感窗口未命中复用。A 的 26 项专项通过，主控在含先前四项性能候选的预演 `c001411c539961be3f27e870633b5747efe5c166` 复跑面积和受管写入 39 项通过，两次 pytest 均退出 0。当前正式批次继续使用冻结修正版，整幅 L 档在 60 秒限制内的结果仍待测量。

## 有界栅格布局索引预研

A 在 `b49025453794331f0a42cb9390a7ed0708c0e7d7` 上隔离提交 `2d14c810000bc04d469a3f70f6638c07c1affc23`。遥感 L 封存首窗的 129 个批次布局各异，一次插桩记录布局索引占 407.7 毫秒 CPU 的 142.5 毫秒。候选在 2、4、8 段层级用占用槽位恢复原整数键顺序和逆索引，每层限 65,536 个槽，且不超过当前环顶点数量的八倍。较稀疏、较宽及更高细化继续走排序。改动限于面积模块和专项测试，公开接口及投影和收敛规则沿用原实现。 [候选提交、补丁与原始证据](p13-05-bounded-raster-topology-evidence.tar.gz)

| 256 × 256 窗口 | A 的 CPU 中位数，毫秒 | 主控复跑 CPU 中位数，毫秒 | 主控复跑墙钟中位数／P95，毫秒 |
| --- | ---: | ---: | ---: |
| DEM M 首窗 | 394.60 → 397.16 | 394.34 → 388.38 | 394.53／407.89 → 388.67／405.82 |
| 遥感 L 首窗 | 397.06 → 344.93 | 393.62 → 333.86 | 393.83／394.91 → 334.03／342.40 |

每次对照在单 CPU 上交替启动每侧三个独立进程，计时覆盖窗口计算。主控复跑的遥感小窗 CPU 中位数下降 15.18%，A 的对照下降 13.13%。DEM M 的差值方向在两次对照中不同。遥感小窗的峰值 RSS 两次对照约在 165 MiB，各自变动不足 1 MiB。环境为 Linux 5.15、Python 3.13.13、NumPy 2.4.4、PROJ 9.5.1、GDAL 3.9.3。

A 与主控在三个封存窗口及合成反例上核对批次顺序、投影点数、坐标、逐格面积、累加结果与错误，复算日志逐字节一致。主控另验证 48 种固定种子布局、192 层的整数键、逆索引及待投影坐标逐字节一致。A 的 29 项面积专项通过，主控将候选补丁应用到 `c001411` 源码后，面积及受管写入 39 项通过，两次退出均为 0。候选保持隔离，可通过撤销该补丁回退。当前证据只覆盖小窗，整幅栅格容量与正式 60 秒配对仍使用排队的固定源码。

```bash
p13_topology=$(mktemp -d)
tar -xzf docs/round2/performance/p13-05-bounded-raster-topology-evidence.tar.gz -C "$p13_topology"
(cd "$p13_topology" && sha256sum -c SHA256SUMS)
mkdir "$p13_topology/source"
tar -xf "$p13_topology/independent/integration-source.tar" -C "$p13_topology/source"
env PYTHONPATH="$p13_topology/source/src" GEOPILOT_TOPOLOGY_FIXTURES="$PWD/benchmarks/raster/area-fixtures" \
  OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 GDAL_NUM_THREADS=1 PROJ_NETWORK=OFF \
  .venv/bin/python -B "$p13_topology/independent/verify.py" > "$p13_topology/replayed.log"
cmp "$p13_topology/replayed.log" "$p13_topology/independent/verify.log"
```

## DEM L 整幅面积容量探针

单独排队的容量探针会让 `977ab41`、`a2d2e68` 和 `b490254` 各在一个原生容器中直接运行生产 `summarize_threshold_area`，输入为同一封存分类 TIFF。总墙钟预算 5,700 秒，单个 worker 上限 1,800 秒。记录完整分类、独立等积面积参考差、CPU、墙钟、峰值 RSS、实际退出和精确源码清单。探针在现行栅格、T14 及 DEM M 原版重试全部退出后取得共用锁，单独归档。正式方法配对继续使用生产 60 秒步骤限制。 [源码清单、启动器、预演及排队记录](p13-05-dem-l-capacity-plan-evidence.tar.gz)

A 的三份源码准备和预演退出 0，忙锁、旧目录、输入或源码失配、子进程超时及收据六项小夹具通过。主控独立预演重放的三条容器命令与 A 记录逐字节相同，Docker 调用为 0，结果根尚未创建。整幅容量计算待正式队列完成。
