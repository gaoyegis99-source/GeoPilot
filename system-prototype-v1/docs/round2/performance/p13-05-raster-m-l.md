# P13-05 栅格 M/L 正式复测

DEM M 在遥感 S 重测结束后于 14:23:57 UTC 启动。原版为 `977ab412d9f6e7558d22a5c431fac0ada90b07be`，修正版为 `a2d2e6874fd7d5d2a239de9d95a927f8505e2ebc`。两侧沿用固定镜像、Python 3.12.3、QGIS 3.44.15 和 `/dev/sda2` ext4。每侧冷、暖各 2 次预热和 5 次正式计量。 [输入清单和执行配方](p13-05-launch-recipes.md) · [遥感 S 配对](p13-05-raster-s.md#遥感-s-重测)

## DEM M 修正版

修正版完成 14 轮，容器退出 1，自比较退出 0。冷态两次预热和五次正式计量均通过。暖态两次预热及一次正式计量因状态查询错误记为失败，三次分别记录 3、1、2 次 HTTP 500 `STORAGE_FAILED`。三次最终均 `SUCCEEDED/PASSED`，七步退出码均为 0。暖态失败的正式样本原始耗时为 64.025 秒，单条失败记录的分位留空。 [原始记录、终态 SQLite 审计及独立复算](p13-05-dem-m-candidate-evidence.tar.gz)

| 正式计量组 | 通过 | 失败 | 成功端到端中位数／P95（秒） | 成功面积步骤中位数（秒） | 成功整轮 RSS partial |
| --- | ---: | ---: | ---: | ---: | ---: |
| 冷态 | 5 | 0 | 46.168／48.919 | 37.312 | 5 / 5 |
| 暖态 | 4 | 1 | 49.798／52.816 | 37.948 | 4 / 4 |

主控从 8 份终态 SQLite 副本核对了 14 个运行、98 个步骤、正式成果登记和检查报告，108 个源文件前后摘要一致。B 复算 14 条原始记录，核对输入、配置回执及七步参数，并确认新运行 ID 与此前核对的 159 个 ID 无交集。14 轮类别 7 均为 4,183,812 像元，NoData 为 2,304 像元，地形及分类最大像元误差均为 0。观测等积面积 418,714,664.9050148 m²，与独立参考相差约 0.000781 m²。

修正版单侧归档可核对输入清单、报告回执、完整冷暖计划和原始冷态前缀，并逐字节重放严格自比较。原版仍在采样，正式配对待两侧完成。

```bash
p13_dem_replay=$(mktemp -d)
tar -xzf docs/round2/performance/p13-05-dem-m-candidate-evidence.tar.gz -C "$p13_dem_replay"
(cd "$p13_dem_replay" && sha256sum -c SHA256SUMS)
.venv/bin/python -B "$p13_dem_replay/proof/replay-side.py" "$p13_dem_replay"
```

## DEM M 原版冷态

原版冷态两次预热及五次正式计量均在 `calculate_area_with_equal_area_projection` 触发 60 秒 `PROCESS_TIMEOUT`，子进程退出 -15，终态为 `FAILED/NOT_CHECKED`。五次正式计量截至失败的端到端中位数为 65.966 秒、P95 为 67.794 秒。第二次预热另记录一次 HTTP 500，最终持久错误仍为面积计算超时。暖态继续采样。 [原始前缀复算](p13-05-dem-m-base-cold.json)
