# P13-05 正式基准工具入库与复算

主控和 A 核对了实际启动器使用的冻结文件。提交 `b35660e3c1f932673020aada29454a98b68103e2` 收录了比较器、配置回执包装器、运行配置指纹、栅格实际请求采集及对应测试。五个 Git 文件的 SHA-256 均与冻结版本一致。原始向量运行器也与 Git 现有版本逐字节一致。 [文件摘要、命令和结果](p13-05-runner-reproducibility.log) · [JUnit](p13-05-runner-reproducibility-junit.xml)

比较器以 `--require-runtime-configuration` 强制核对两侧回执、报告字节及配置。包装器从只读源码树采集配置，完整报告终态之后写入回执。栅格采集器在停计时后，从实际工作目录读取归一化请求并核对运行身份和执行前缀。原始 JSONL 在便携证据中有独立的 SHA-256 校验。 [比较器](../../../scripts/bench/run_memory_profile.py) · [包装器](../../../scripts/bench/run_with_runtime_configuration.py) · [栅格采集器](../../../scripts/bench/_raster_workloads.py)

- 对现有 M 档四组、DEM S 正式配对和 L 点相交原版自比较逐一回放，六份结果均与原输出逐字节一致，退出 0。缺回执和回执签发后报告字节变化的两个反例均被拒绝，退出 1。
- Python 3.13.13 上，栅格请求和状态观测专项在审计分支与独立集成预演各 4 项通过、10 项未选，退出 0。Python 3.12.3 原生镜像上，矢量和栅格只读包装入口分别运行 `--help` 并退出 0，均未创建报告或配置回执。
- 两个性能候选提交在独立工作树应用通过。30 个变更文件与冻结候选逐字节相同，并核对了先前集成的取消轮询修复。性能代码集成仍待完整 P13-05 配对及回归。

回滚可撤销 `b35660e`。正式基准输出保留在原有独立目录，当前测量沿用已冻结的运行器。

本轮实际使用的两个启动器与输入清单已封存，解包预演的容器命令与原输出一致。 [启动配方](p13-05-launch-recipes.md)

## 从 Git 重建冻结源码

主控在空的 tmpfs 目录导出原版和修正版，覆盖实际使用的五个运行文件，并单独导出严格比较器。重建所得 SHA-256 文件清单与运行前冻结清单逐字节一致，原版 1,466 个、修正版 1,473 个常规文件全部匹配，退出 0。两侧的原始清单随点相交证据归档提供。 [点相交归档](p13-05-vector-l-point-evidence.tar.gz)

```bash
p13_repo=$(git rev-parse --show-toplevel)
p13_stage=$(mktemp -d)
p13_overlay=b35660e3c1f932673020aada29454a98b68103e2
mkdir "$p13_stage/base" "$p13_stage/candidate" "$p13_stage/runner"
git -C "$p13_repo" archive 977ab412d9f6e7558d22a5c431fac0ada90b07be:system-prototype-v1 \
  | tar -x -C "$p13_stage/base"
git -C "$p13_repo" archive a2d2e6874fd7d5d2a239de9d95a927f8505e2ebc:system-prototype-v1 \
  | tar -x -C "$p13_stage/candidate"
for p13_file in run_vector.py _vector_reference.py _vector_road_profile.py _runtime_config.py run_with_runtime_configuration.py; do
  git -C "$p13_repo" show "$p13_overlay:system-prototype-v1/scripts/bench/$p13_file" > "$p13_stage/runner/$p13_file"
  cp "$p13_stage/runner/$p13_file" "$p13_stage/base/scripts/bench/"
  cp "$p13_stage/runner/$p13_file" "$p13_stage/candidate/scripts/bench/"
done
git -C "$p13_repo" show "$p13_overlay:system-prototype-v1/scripts/bench/run_memory_profile.py" \
  > "$p13_stage/runner/run_memory_profile.py"
mkdir "$p13_stage/seals"
tar -xzf docs/round2/performance/p13-05-vector-l-point-evidence.tar.gz -C "$p13_stage/seals"
p13_point="$p13_stage/seals/t13-points-intersects"
for p13_role in base candidate; do
  p13_attempt=$(cat "$p13_point/$p13_role/COMPLETED")
  (cd "$p13_stage/$p13_role" && find . -type f -print0 | LC_ALL=C sort -z | xargs -0 sha256sum) \
    | diff -u "$p13_point/$p13_role/$p13_attempt/source-tree.sha256" -
done
```

## 基准状态观察失败分类

遥感 S 重测原版有三次状态观察失败。三次均取得同一运行的 `SUCCEEDED/PASSED`，也通过独立成果验算。冻结比较器将其故障类型记为 `unspecified`。现行比较器在原始状态查询错误、`StatusObservationFailure`、运行身份、终态和成果验算均有证据时，输出 `STATUS_OBSERVATION_FAILED`。计量仍归入失败组，持久 GIS 错误码优先，成功样本比较条件沿用原规则。正式旧批次使用各自归档中的冻结工具复现。 [遥感 S 原始记录](p13-05-raster-s.md#遥感-s-重测)

主控在 `6411db937e51e24782529ae690d7bacd03f49dcb` 上加入两处文件变更。Linux 5.15、Python 3.13.13、pytest 8.3.4。专项修正前 1 失败、5 通过，退出 1。修正后联合验证 8 通过、12 未选，退出 0。用现行工具重放遥感 S 和 DEM L 的严格配对均退出 0。遥感 S 冷组和暖组分别有 2、1 条分类更新，其余 JSON 字段一致。DEM L 的超时结果逐字节一致。回滚可撤销本项提交。 [命令、红绿日志、JUnit 和两份实际比较](p13-05-status-observation-classification-evidence.tar.gz)

## 状态查询失败耗时采集

现行栅格采集器在每条 `status_poll_errors` 记录中保留 `elapsed_ns`，范围为客户端发出该次状态查询到收到响应的单调时钟耗时。HTTP 状态、响应正文、连续失败上限和试次计量范围沿用原逻辑。它与生产诊断里的服务端 `elapsed_ms` 各有计时边界。冻结运行沿用原采集器，原始旧记录继续可读。 [生产状态诊断](p13-07.md#热回滚日志下的只读状态查询)

主控在 `4a293738037d404051405f960ea731208174d02c` 上修改采集器和已有专项测试。Linux 5.15、Python 3.13.13、SQLite 3.51.2。缺字段反例退出 1，修正后 10 项通过、10 项未选，退出 0。实际生产路由探针使用临时库和 `TestClient`，先取得 HTTP 500，再观察到 `SUCCEEDED/PASSED`。独占锁的客户端记录为 5.040 秒，服务端主码／扩展码为 5／5。缺失库的客户端记录为 39.841 毫秒，服务端主码／扩展码为 14／14。探针退出 0。客户端耗时用于定位等待范围，底层错误仍以服务端诊断为准。回滚可撤销本次采集改动。 [实际命令、JUnit 和探针结果](p13-05-status-poll-timing-evidence.tar.gz)

## 状态查询失败序号

D 在冻结栅格采集器中离线复现。两组各含三次 HTTP 500 的查询序列，一组交错失败，另一组有两个相邻失败，生成相同的错误列表。在已封存遥感 L 原版的隔离副本移除一轮真实超时同时发生的四条 HTTP 500，冻结验收仍通过，严格比较结果逐字节一致。配置回执绑定 `report.json`，原始 `trials.jsonl` 与日志由归档单独校验。旧 DEM M 中断的三连失败依据显式中止记录。 [冻结样本、原始命令与复放](p13-05-status-poll-order-evidence.tar.gz)

现行采集器在每条失败记录加入从 1 开始的 `request_index`，计入该试次所有状态 GET。失败序号的间隔可用于识别中间成功读取。轮询间隔、连续失败上限、耗时统计和 API 契约保持既有口径。主控在 `217320b` 上复现两组同耗时反例，缺失序号时两项失败，修正后相关专项 12 通过、10 未选，退出 0。正式重试沿用冻结采集器。回滚可撤销本次采集改动。
