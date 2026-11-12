# GeoPilot 第二轮候选包与离线复现

候选包从 Git 中读取精确源提交和验证证据提交，包含源码、配置、锁定依赖文件、测试、样本、基准配方和验证记录。正式候选的来源、验证和证据在第二轮阶段退出时固定。部署端负责 GIS 计算，用户端使用浏览器。 [安装矩阵](../round2/deployment/p16-02.md)

## 固定验证证据

在精确源提交的独立副本执行后端、前端和固定 GIS 用例。保存每组实际命令、退出码、环境版本、日志和原始 JUnit。日志要包含单独的 `commit <完整源 SHA>` 行和实际退出码行，例如 `exit 0`。固定 GIS 组按 T01–T16 现有映射运行并核对覆盖。 [固定用例复验和命令](../round2/audit/p12-07.md)

验证回执路径为 `artifacts/reports/final/round2-validation.json`。它记录以下字段。

| 字段 | 内容 |
|---|---|
| `schema_version` | 整数 1 |
| `source_commit` | 本次验证的完整源提交 |
| `suites` | 至少含 `backend`、`frontend` 和 `fixed_gis`。每条写入 `name`、命令参数数组 `command`、版本对象 `environment`、整数 `exit_code`、原始 `junit` 和 `log` 路径 |
| `evidence` | 纳入候选包的正式验收、样本复核、性能及部署记录路径 |
| `reproduction` | 候选包需要校验本地链接的 Markdown 复现材料路径 |

回执路径相对 Git 根目录。提交验证回执、日志和报告后，源码作用域须与源提交逐文件相同。生成器读取 JUnit 的用例实际状态，核对汇总计数，并从 Git 中核对复现文档的本地链接。固定 GIS 报告必须为已验收的 T01–T16 映射逐个提供唯一测试节点。该映射随源提交固定，离线验证也逐项核对。

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

后端按安装矩阵设置 QGIS 环境并在独立项目目录启动。复跑时按回执中的环境和参数执行三个验证组，比较 JUnit 和关键 GIS 数值。 [工作台启动说明](../../README.md#启动工作台) · [部署矩阵](../round2/deployment/p16-02.md)
