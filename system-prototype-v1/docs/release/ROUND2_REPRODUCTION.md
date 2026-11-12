# GeoPilot 第二轮候选包与离线复现

候选包从 Git 中读取精确源提交和验证证据提交，包含源码、配置、锁定依赖文件、测试、样本、基准配方和验证记录。正式候选的来源、验证和证据在第二轮阶段退出时固定。部署端负责 GIS 计算，用户端使用浏览器。 [安装矩阵](../round2/deployment/p16-02.md)

## 固定验证证据

在精确源提交的独立副本执行后端、前端和固定 GIS 用例。以下命令从已冻结的原型目录运行。使用已按测试依赖安装的 Python、已核验的 QGIS 入口和已锁定的 Node/npm。完整后端还需要可导入的 Psycopg 3.3.4、可用的本机 Docker，以及 PostGIS 和隔离试点在各自验收中使用的镜像。将 Psycopg 装在独立目录时传入 `--python-path`。已有离线 npm 缓存时传入 `--offline-npm-cache`。 [后端基线和 PostGIS 环境](../round2/audit/p12-05.md) · [固定用例复验](../round2/audit/p12-07.md) · [隔离试点镜像](../round2/agent/p15-06.md)

```bash
release_repo=$(git rev-parse --show-toplevel)
release_source=$(git rev-parse HEAD)
release_staging=$(mktemp -d)
release_python="$PWD/.venv/bin/python"
git -C "$release_repo" worktree add --detach "$release_staging/source" "$release_source"
"$release_python" "$release_staging/source/system-prototype-v1/scripts/quality/run_round2_validation.py" \
  --commit "$release_source" --python "$release_python" \
  --qgis-process "$GEOPILOT_QGIS_PROCESS" --output "$release_staging/validation" \
  --evidence system-prototype-v1/plans-round2/06_PROGRESS/ACCEPTANCE_LOG.md
```

为每项正式验收、性能、部署和用户研究证据增加 `--evidence`。工具执行锁定的 npm 安装和构建、Vitest、后端完整收集与执行，以及 T01–T16 的独立固定用例。每组写入实际命令、工作目录、日志和原始 JUnit，回执记录环境与退出码。失败时已有原始记录留在输出目录。成功后将输出目录中的 `round2-validation.json` 和整个 `round2-validation/` 复制到证据提交的 `artifacts/reports/final/`。保持验证使用的源码和回归测试输入逐文件一致。

验证回执路径为 `artifacts/reports/final/round2-validation.json`。它记录以下字段。

| 字段 | 内容 |
|---|---|
| `schema_version` | 整数 1 |
| `source_commit` | 本次验证的完整源提交 |
| `suites` | 至少含 `backend`、`frontend` 和 `fixed_gis`。每条写入 `name`、命令参数数组 `command`、版本对象 `environment`、整数 `exit_code`、原始 `junit` 和 `log` 路径。后端的 `collection` 写入完整收集的 `command` 和 `log`。 |
| `evidence` | 纳入候选包的正式验收、样本复核、性能及部署记录路径 |
| `reproduction` | 候选包需要校验本地链接的 Markdown 复现材料路径 |

回执路径相对 Git 根目录。生成器核对日志的源提交、实际命令和最终退出行，读取 JUnit 实际结果并与后端完整收集逐节点比对。固定 GIS 报告须为 T01–T16 映射逐个提供唯一节点。后端和前端报告中的跳过独立计数，发布评估根据原始 JUnit 核对原因。生成器检查列入复现清单的文档及其直接本地链接。回归测试使用的计划、历史审计材料、模板和合成记录随源码固定。离线验证重读日志、后端收集记录和固定 GIS 映射。

## 生成与检验

从原型目录执行。将 `release_source` 和 `release_evidence` 设为已经固定的两个提交。归档路径使用新文件名。

```bash
.venv/bin/python artifacts/reports/final/delivery_manifest.py --round2 \
  --source-ref "$release_source" --evidence-ref "$release_evidence" \
  --archive /path/to/geopilot-round2.tar.gz --write
.venv/bin/python scripts/quality/verify_delivery_manifest.py \
  --round2-manifest artifacts/reports/final/round2-delivery-manifest.json
sha256sum /path/to/geopilot-round2.tar.gz artifacts/reports/final/round2-delivery-manifest.json
```

离线主机先核对交付方公布的归档 SHA-256，在空目录解包。可用可信的校验脚本对解包根目录逐文件复核，无需 Git 历史。离线验证同时重读归档内的日志和 JUnit。

```bash
mkdir /path/to/round2-extracted
tar -xf /path/to/geopilot-round2.tar.gz -C /path/to/round2-extracted
cd /path/to/round2-extracted/system-prototype-v1
python scripts/quality/verify_delivery_manifest.py \
  --round2-manifest artifacts/reports/final/round2-delivery-manifest.json \
  --offline-root /path/to/round2-extracted
```

## 离线启动和测试

准备安装矩阵中对应的 Linux GIS 后端、已验证的 QGIS 入口、与锁定依赖相符的 Python wheels 和 npm 缓存，或使用已有的同版本测试环境。依赖分发包随部署环境准备。候选归档携带依赖版本及源码。

```bash
python -m venv .venv
.venv/bin/python -m pip install --no-index --find-links /path/to/wheelhouse -r requirements-test.txt
cd frontend
npm ci --offline --cache /path/to/npm-cache
npm run build
```

后端按安装矩阵设置 QGIS 环境并在独立项目目录启动。离线复跑时按回执中的环境和参数执行三个验证组，使用本机 Python、QGIS 与临时目录的路径，并把 `GEOPILOT_QGIS_PROCESS` 和 `GEOPILOT_BENCH_QGIS` 设为同一入口。后端完整收集、固定 GIS 节点和原始 JUnit 均在包内，可用于比较。 [工作台启动说明](../../README.md#启动工作台) · [部署矩阵](../round2/deployment/p16-02.md)
