# P13-03 S/M 原驱动归档与独立复算

S、M 首段和 M 续段三份原生容器挂载快照中的 `run_vector.py` 与 `_vector_reference.py` 分别逐字节相同。主控直接从已完成的 S 挂载快照归档这两个文件。两次归档逐字节一致，SHA-256 为 `28419e97039836130f5f87eab1488183fd222a863440010ffeced02f9bec1413`。在冻结生产源 `37cb329be04eaddfeb23ac62789926d0fcdb63aa` 的独立目录叠加归档，成员摘要一致，原驱动 `--help` 在 Linux、Python 3.13.13 下退出 0。 [原驱动归档](p13-03-SM-runner-snapshot.tar.gz) · [成员校验清单](p13-03-SM-runner-files.sha256) · [原始验证输出](p13-03-SM-replay.log)

正式 S/M 采样使用归档版本。L 采样的驱动后续增加校正道路输入、输入清单标识和方案准备失败记录。独立结果校验器的 SHA-256 一致。重做 S/M 的生产测量时，从拥有冻结提交的 Git checkout 准备新的挂载目录。

```bash
prototype_root="$PWD"
replay_root=$(mktemp -d /mnt/datab/geopilot-vector-sm-replay.XXXXXX)
mkdir "$replay_root/source"
git -C "$(git rev-parse --show-toplevel)" archive 37cb329be04eaddfeb23ac62789926d0fcdb63aa system-prototype-v1 |
  tar -x -C "$replay_root/source"
tar -xf "$prototype_root/docs/round2/performance/p13-03-SM-runner-snapshot.tar.gz" \
  -C "$replay_root/source/system-prototype-v1"
(
  cd "$replay_root/source/system-prototype-v1"
  sha256sum -c "$prototype_root/docs/round2/performance/p13-03-SM-runner-files.sha256"
)
```

从对应批次的 `p13-03-S-command.log`、`p13-03-M-command.log` 或 `p13-03-M-cont-command.log` 取第一行的实际容器命令。把其中 `/project` 的宿主挂载改为上述 `$replay_root/source/system-prototype-v1`，`/scratch` 的宿主挂载改为 `$replay_root`。原测量环境为 Ubuntu 24.04、Python 3.12.3、QGIS 3.44.15，实际命令还记录了镜像、原生 Python 和 QGIS 路径。保持生产时限和组内采样参数。该路径需要冻结提交及运行时依赖，已归档数据的复算只需要 Python 和证据文件。

F 从四批 gzip JSONL 重算组级与步骤级分布，主控复跑同一命令得到逐字节相同的 JSON，退出 0。四批共有 325 个唯一运行。S 的 140 个运行均通过。M 首段 115 个通过、25 个因预算未发起，续段 28 个通过，拼接后的 20 个正式组各取同一批次的完整样本。首批 L 的 42 个运行均失败，六组无成功性能分位，均未执行独立几何与 ID 验算。脚本的范围截至首批 L。 [独立复算脚本](p13-03-archive-audit.py) · [复算 JSON](p13-03-archive-audit.json)

```bash
nice -n 19 taskset -c 31 .venv/bin/python \
  docs/round2/performance/p13-03-archive-audit.py docs/round2/performance
```

## L 后续批次独立复核

主控从正在写入的原始 JSONL 冻结前 74 条完整记录，与首批 L 的环境和输入清单对照。两批使用相同源提交、驱动、QGIS 和输入摘要。首批与后续记录共有 116 个唯一 run ID。后续的十组已完成全部冷热采样键，其余样本继续采集。点任务在裁剪超时，其余已观测任务在空间关系超时。逐条核对持久终态、计划步骤、进程退出码和资源观测覆盖。 [复核脚本](p13-03-L-continuation-audit.py) · [原始复算结果](p13-03-L-continuation-audit-preview.json)

同一批原始字节压缩归档后重跑输出一致。注入重复 run ID、移除实际进程超时标记，以及用未采完的样本执行正式审计，均退出 1。正式审计还要读取全部 98 条和最终汇总。当前输出状态为 `PREVIEW`。原始压缩样本和命令保存在 `artifacts/test-results/p13-03-L-continuation-audit.wnPNCG/`。 [复跑及异常验证](p13-03-L-continuation-audit.log)
