# 矢量基准

从原型目录执行。使用 PATH 中的 `qgis_process`，也可传入其路径。每次执行创建一个新目录，保留输入文件、运行记录和汇总。重复执行时使用新的输出路径。

```bash
python scripts/bench/run_vector.py --profile benchmarks/datasets/vector.yaml \
  --qgis-process /usr/bin/qgis_process --output /tmp/geopilot-vector
```

`--tier`、`--workload` 和 `--cache-state` 可重复指定。默认覆盖 S/M/L、全部十个工作负载和冷暖两种状态。T13 的工作负载为 `t13-{points,lines,polygons}-{intersects,within}`，T14 为 `t14-{split_by_area,centroid,largest_overlap}`，完整道路方法为 `road`。

`--probe` 默认在暖态每项运行一次用于验证入口和独立结果参考。正式执行按 [基准协议](../README.md) 取两种缓存状态，每组先预热两次，再测五次。默认每次最多等候 600 秒，达到期限后向正式运行取消接口提交请求。可用 `--budget-seconds` 设定停止发起后续样本的时间点，已开始的样本继续到达终态。退出 0 表示此次选择的运行均通过，退出 1 表示存在失败或未运行的样本，退出 2 表示参数、输入或环境错误。

## 原生 QGIS 容器

`run-native.sh` 使用包含 SQLite 争用恢复的 `37cb329` 源代码快照、当前基准脚本以及 [P16-02 的 QGIS 环境](../../docs/round2/deployment/p16-02.md)。准备镜像、原生 QGIS CLI 绑定和 `.environments/p13-native/venv` 后，执行以下命令。传入的目录须不存在。

```bash
scratch="$(mktemp -d /tmp/geopilot-vector.XXXXXX)"
bash benchmarks/vector/run-native.sh "$scratch/S" --tier S
```

容器内 `qgis_process` 与 Python 服务处于同一个进程观测范围。脚本禁用容器网络，记录原生工具和脚本摘要、容器命令、挂载盘和退出码。若使用宿主侧 Docker 包装器，设置 `GEOPILOT_TELEMETRY_QGIS_SCOPE=container`，由遥测将不可观测的容器资源标为 unavailable。

## L 档道路位置校正

原始 L 道路输入的范围超出 EPSG:32650 使用区，正式方案确认返回 422。独立配方 `road-l-utm50n-south@1` 保持 128 × 128 单元、4,000 m 间距、道路 33 顶点、256 B 属性和 EPSG:32650，将北向原点固定在 3,880,000 m。该配方只应用于 L 道路工作负载。 [原范围与生产校验](../../docs/round2/performance/p13-03-L-coverage.json)

```bash
bash benchmarks/vector/run-native.sh "$scratch/L-road-probe" --tier L --probe --workload road \
  --road-profile benchmarks/vector/road-l-utm50n-south.v1.yaml
```

运行记录注明 `input_profile` 和对应清单 SHA-256。两份输入清单分别保留原始 P13-01 数据和校正后道路 GeoPackage 的物理与逻辑摘要。未指定校正配方时继续使用原始 P13-01 道路。

## 结果

- `environment.json` 与 `inputs.json` 记录环境、源代码版本、测量口径和输入生成前后校验。完整的每档独立参考在 `input-*/manifest.json`。
- `samples.jsonl` 保存每次预热、测量和试跑的阶段、run ID、终态、步骤退出码、工具版本、产物来源、独立校验结果、日志和原生遥测。每个步骤最多保留日志最后 4 KiB。运行工作目录于进程组结束后清理。
- `summary.json` 与 `summary.csv` 汇总每档、工作负载和缓存状态。 `step-summary.csv` 按实际步骤和精确工具版本汇总。至少三个通过且有数值的正式样本才计算中位数和 P95，数值来源为 partial 的数量单列。
- 端到端墙钟从提交确认方案开始，止于第一次观察到持久化最终状态，轮询间隔为 100 ms。独立几何和数值检查在量测之外执行。步骤墙钟和子进程树资源来自原生遥测。单独的检查时间标为 unavailable，使用 finalization 字段记录包含检查和产物登记的开销。
- 临时文件峰值按运行目录采样，排除正式输出目录及确认方案声明的正式产物，并按 inode 去重。盘读写的 partial 值是累计观测下界，RSS 为采样峰值。OS 页缓存未控制。
