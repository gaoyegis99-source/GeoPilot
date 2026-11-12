# P13-05 正式启动配方归档

归档保存本轮实际使用的 L 档矢量和栅格启动器、各自的核验脚本与预演记录、两侧冻结源码清单，以及 DEM S/M/L 和遥感 S/L 五份原始输入清单。归档内部文件及五组输入清单摘要均通过校验。 [启动配方归档](p13-05-launch-recipes.tar.gz) · [解包复验](p13-05-launch-recipes-verification.log)

在原测量主机的原型目录执行。解包后，矢量六条及栅格十条容器命令的预演输出与本机正式启动配方逐字节一致，两个入口均退出 0。

```bash
p13_launch=$(mktemp -d)
tar -xzf docs/round2/performance/p13-05-launch-recipes.tar.gz -C "$p13_launch"
(cd "$p13_launch" && sha256sum -c SHA256SUMS)
bash "$p13_launch/p13-05-vector-l-plan.a2d2e68.C905zE/run-l.sh" --dry-run
.venv/bin/python -B "$p13_launch/p13-05-raster-plan-a2d2e68.TONYtP/run-raster-pairs.py" --dry-run
```

正式启动依赖两个冻结源码树、固定镜像、只读 Python/QGIS 绑定及记录的本机路径和磁盘 UUID。矢量入口按固定配置生成 L 档输入并核对指纹。栅格入口只读挂载 P13-04 的完整原始输入目录，归档中的五份清单供核对。两侧实际结果及退出码随对应配对记录交付。 [冻结源码重建](p13-05-runner-reproducibility.md#从-git-重建冻结源码) · [原生镜像和绑定恢复](../deployment/p16-06-tooling.md#原生基准绑定文件恢复) · [栅格输入来源](p13-04.md)

归档另含剩余四组栅格实际队列的八条预演命令，以及两个冻结提交的增量 Git bundle。接收仓库需要包含共同祖先 `b75090bb302bfb9174d229cb23c5c5fb63b9e565` 和运行器覆盖提交 `b35660e3c1f932673020aada29454a98b68103e2`。主控以审计分支初始化独立仓库，验证两个冻结提交起初缺失，导入后精确恢复，再重建两侧源码并逐文件核对清单，退出 0。 [栅格队列](p13-05-raster-s.md)

```bash
git bundle verify "$p13_launch/frozen-source-revisions.bundle"
git fetch --no-tags "$p13_launch/frozen-source-revisions.bundle" \
  refs/heads/codex/p13-05-preflight-base:refs/heads/codex/p13-05-preflight-base \
  refs/heads/codex/p13-05-candidate-lazy-staging:refs/heads/codex/p13-05-candidate-lazy-staging
```

局部裁回与分区重投影暂存修补另有增量 Git bundle，前置提交为 `a2d2e6874fd7d5d2a239de9d95a927f8505e2ebc`。在只从审计分支和上一份 bundle 初始化的仓库导入两项修补后，按原五个文件覆盖重建 T14 候选树，1,473 个文件的摘要清单与正式预演逐字节相同。归档的 `source-revisions/` 包含 bundle 及实际复验日志。 [后续候选归档](p13-05-t14-local-clip-evidence.tar.gz)

```bash
p13_followup=$(mktemp -d)
tar -xzf docs/round2/performance/p13-05-t14-local-clip-evidence.tar.gz -C "$p13_followup"
(cd "$p13_followup" && sha256sum -c SHA256SUMS)
git bundle verify "$p13_followup/source-revisions/followup-source-revisions.bundle"
git fetch --no-tags "$p13_followup/source-revisions/followup-source-revisions.bundle" \
  refs/heads/codex/p13-05-t14-local-clip:refs/heads/codex/p13-05-t14-local-clip \
  refs/heads/codex/p13-05-managed-land-use-zones:refs/heads/codex/p13-05-managed-land-use-zones
```
