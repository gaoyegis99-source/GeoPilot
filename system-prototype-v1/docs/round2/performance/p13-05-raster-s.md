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

主控另用原版状态路由和同一数据库副本验证 HTTP 行为。另一连接执行 `BEGIN IMMEDIATE` 时 GET 返回 200，持有 `BEGIN EXCLUSIVE` 时在约 5 秒后返回 500 `STORAGE_FAILED`，捕获到的原始异常为 `SQLITE_BUSY`。释放锁后 GET 恢复为 200。探针保留原版响应，并核对原始数据库摘要。 [探针](p13-05-raster-s-status-probe.py) · [原始输出](p13-05-raster-s-status-probe.json)

原版退出 1，正式冷态为 1 通过、2 失败、2 缺失，正式暖态 5 次均未运行。包装器未生成完整配置回执，批次启动器退出 2。离线强制比较也因缺少回执退出 1。保留单侧分布和原始失败样本，遥感 S 前后差值待两侧用新目录重采样。

## 遥感 S 重测

重测于 14:16:19–14:23:57 UTC 执行。两侧各完成 14 轮，原版容器退出 1，修正版退出 0，配对启动器退出 0。严格比较确认两侧输入指纹、原生运行环境、八步执行参数和绑定报告的配置回执一致。主控与 B 核对了 28 个不同运行 ID，与首轮 47 个运行 ID 无交集。配对及两侧自比较均从原始证据离线重放，结果逐字节相同。 [严格配对](p13-05-remote-s-rerun-comparison.json) · [原始报告和独立复算](p13-05-remote-s-rerun-evidence.tar.gz) · [复算日志](p13-05-remote-s-rerun-verification.log)

| 版本和正式计量组 | 通过 | 失败 | 成功中位数（秒） | 成功 P95（秒） | 成功 RSS partial |
| --- | ---: | ---: | ---: | ---: | ---: |
| 原版冷态 | 3 | 2 | 8.473 | 8.477 | 3 / 3 |
| 原版暖态 | 4 | 1 | 14.228 | 21.501 | 3 / 4 |
| 修正版冷态 | 5 | 0 | 8.277 | 8.295 | 5 / 5 |
| 修正版暖态 | 5 | 0 | 8.772 | 24.817 | 5 / 5 |

原版三次失败均为 `StatusObservationFailure`，冷态第 2、5 次正式计量及暖态第 5 次正式计量分别遇到 4、1、1 次 HTTP 500 `STORAGE_FAILED`。三次都到达 `SUCCEEDED/PASSED`，八步退出码均为 0。失败样本耗时原值为冷态 40.668、29.311 秒和暖态 34.321 秒。每组不足三条失败记录，比较器保留失败次数、原始耗时和 RSS 覆盖，失败分位留空。两侧正式成功样本数不同，成功耗时和资源差值也留空。

独立正确性覆盖 28 轮。每轮分类 1、2、3 各 6,144 像元，NoData 为 18,432 像元，栅格最大像元误差为 0。等积面积合计 16,602,074.596798936 m²，与独立参考相差约 0.000088 m²。D 从 8 个终态 SQLite 副本核对原版 14 个运行、112 个步骤和正式成果登记，全部与原始结果一致，114 个源文件前后摘要相同。此批 HTTP 记录未保存具体异常和锁持有者。DEM M 于 14:23:57 UTC 接续开始，随后为 DEM L、遥感 L 和按面积分配候选复测。 [后续栅格复测](p13-05-raster-m-l.md) · [状态查询诊断](p13-07.md#状态查询错误诊断)

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

遥感 S 重测归档独立解包。输入清单指纹、14 次计划、报告回执和两侧及配对比较均在解包目录重放。

```bash
p13_remote_replay=$(mktemp -d)
tar -xzf docs/round2/performance/p13-05-remote-s-rerun-evidence.tar.gz -C "$p13_remote_replay"
(cd "$p13_remote_replay" && sha256sum -c SHA256SUMS)
.venv/bin/python -B "$p13_remote_replay/proof/replay.py" "$p13_remote_replay" remote-s
```

L 档矢量于 12:25 UTC 启动，依次采样点相交、线相交和按面积分配。运行入口为 `bash artifacts/test-results/p13-05-vector-l-plan.a2d2e68.C905zE/run-l.sh --run`，输出在 `/mnt/datab/geopilot-p13-05-vector-l.a2d2e68.C905zE/`。12:36 UTC，遥感 S、DEM M、DEM L、遥感 L 排队等候同一测量锁。四组预演通过，核对了八条容器命令、四条强制回执比较命令和四个输入指纹。队列每组单独调用栅格启动器，首组和后续组使用新输出根 `artifacts/test-results/p13-05-raster-pairs.a2d2e68.after-vector-l/`，旧样本继续保留。P13-05 仍在执行。 [修正版矢量 M](p13-05-vector-m-revised.md)

状态路由探针使用原版源码运行，退出 0。以首轮原始遥感证据中的数据库为输入，测量副本由临时目录在退出时清理。

```bash
p13_db=artifacts/test-results/p13-05-raster-pairs.a2d2e68.TONYtP/remote-s/base/attempt.28na5exq/evidence/samples/S/method/process_cold/05-measurement/service/project.sqlite
PYTHONPATH=artifacts/test-results/p13-05-revised-pair-source.a2d2e68/base/src \
  .venv/bin/python -B docs/round2/performance/p13-05-raster-s-status-probe.py "$p13_db"
```

本批提交涉及证据、复算脚本及进度记录，回滚可撤销相应证据提交。
