# P13-05 修正版栅格 S 档正式复测

主控串行执行了 `dem-s` 和 `remote-s`。原版为 `977ab412d9f6e7558d22a5c431fac0ada90b07be`，修正版为 `a2d2e6874fd7d5d2a239de9d95a927f8505e2ebc`。两侧使用同一原生镜像 `sha256:c618d0d9d91d0cf473d61e5fc284aa335e87caabd51381b73cd61b2d0f93cb87`、Python 3.12.3、QGIS 3.44.15 和 `/dev/sda2` ext4。每侧计划冷、暖各 2 次预热和 5 次正式计量。主控和 B 分别复算了 47 个唯一运行 ID。 [原始报告、命令和配置回执](p13-05-raster-s-evidence.tar.gz) · [复算结果](p13-05-raster-s-comparison.json) · [验证日志](p13-05-raster-s-verification.log)

## DEM S

两侧各 14 次均 `SUCCEEDED/PASSED`，两个容器退出 0。严格比较器确认输入清单、环境、配置回执及逐步归一化请求一致。原始分类均有 259,956 个 7 类像元和 144 个 NoData 像元，`EPSG:32650` 至 `EPSG:6933` 的等积面积均为 26,016,403.376894236 m²，与独立参考相差约 0.000048 m²。

| 计量组 | 端到端中位数（秒） | 端到端 P95（秒） | 面积统计步骤中位数（秒） | RSS partial（原版 / 修正版） |
| --- | ---: | ---: | ---: | ---: |
| 冷态 | 9.664 → 8.241 | 9.883 → 9.244 | 4.784 → 3.434 | 5 / 3 |
| 暖态 | 9.657 → 8.430 | 9.845 → 8.617 | 4.795 → 3.479 | 4 / 4 |

RSS 只保留各侧观测值。临时文件峰值采样完整，冷态中位数均为 1,446 字节，暖态为 1,076 → 930 字节。

## 遥感 S

修正版 14 次均 `SUCCEEDED/PASSED`，容器退出 0。冷、暖单侧端到端中位数分别为 8.274、8.897 秒，P95 分别为 8.326、9.450 秒。RSS partial 为 5/5 和 4/5。修正版及原版可验算的四次运行中，三类各 6,144 个像元，NoData 为 18,432 个像元，面积合计 16,602,074.596798936 m²，与独立参考相差约 0.000088 m²。NDVI、质量掩膜及分类的最大像元误差为 0。

原版执行到冷态第 5 次。状态 GET 共返回九次 HTTP 500 `STORAGE_FAILED`。第 2 次预热和第 1 次正式计量因状态观察错误判为失败，持久运行及八步计算均成功并通过独立验算。第 3 次正式计量连续三次状态读取失败，触发停止，取消请求返回 HTTP 202。随后以只读方式检查该次数据库，确认 `align_two_dates` 因 `database is locked` 中断，终态为 `FAILED/NOT_CHECKED`，错误码为 `RUN_INTERRUPTED`。数据库导出记录查询及摘要。HTTP 500 的逐次 SQLite 异常和锁持有者未记录。

主控用原版代码、数据库副本和独立的 `BEGIN EXCLUSIVE` 连接做了受控验证。子进程写出结果后，父进程的取消轮询在 5.008 秒后抛出 `SQLITE_BUSY`。无锁对照正常返回，释放锁后已提交的取消请求能生成标记。该实验定位了一个可复现的失败路径，正式样本中的锁持有者仍未确认。 [调用栈和结果](p13-05-raster-s-verification.log)

原版退出 1，正式冷态为 1 通过、2 失败、2 缺失，正式暖态 5 次均未运行。包装器未生成完整配置回执，批次启动器退出 2。离线强制比较也因缺少回执退出 1。保留单侧分布和原始失败样本，遥感 S 前后差值待两侧用新目录重采样。

## 命令与复算

在原型根目录执行。首次栅格批次原始根为 `artifacts/test-results/p13-05-raster-pairs.a2d2e68.TONYtP/`。归档带有冻结比较器，独立解包复算退出 0，并与实时证据复算逐字节相同。

```bash
.venv/bin/python -B artifacts/test-results/p13-05-raster-plan-a2d2e68.TONYtP/run-raster-pairs.py \
  --run --case dem-s --case remote-s
p13_replay=$(mktemp -d)
tar -xzf docs/round2/performance/p13-05-raster-s-evidence.tar.gz -C "$p13_replay"
(cd "$p13_replay" && sha256sum -c SHA256SUMS)
.venv/bin/python -B docs/round2/performance/p13-05-raster-s-audit.py \
  "$p13_replay" "$p13_replay/run_memory_profile.py"
```

L 档矢量于 12:25 UTC 启动，依次采样点相交、线相交和按面积分配。运行入口为 `bash artifacts/test-results/p13-05-vector-l-plan.a2d2e68.C905zE/run-l.sh --run`，输出在 `/mnt/datab/geopilot-p13-05-vector-l.a2d2e68.C905zE/`。P13-05 仍在执行，剩余范围包括遥感 S 新配对、DEM M/L 和遥感 L。本批提交涉及证据、复算脚本及进度记录，回滚可撤销该证据提交。 [修正版矢量 M](p13-05-vector-m-revised.md)
