# P12-01 审计基线

- 执行与验收：主控。历史基线为 Git 提交 `80d724f35fba31cacb9d3f740265b2713c6be6f4`。
- 原工作树 `main` 有 165 个未暂存的 V1 计划文件删除，以及 1 个未跟踪的第二轮计划目录。完整路径及状态单列于 JSON。独立审计分支 `round2/p12-audit` 从该提交检出，复制第二轮计划至 `plans-round2/`。
- 清单覆盖 606 个提交对象：src 155、config 57、frontend 57、tests 160、docs 147、artifacts 30。哈希基于提交的原始 blob，记录 Git 模式。工作树文件状态另存。
- 历史证据索引的统计包含不同的目录或文件类型口径。本次计数以六个指定目录中的全部 Git 文件为准。 [原始快照](baseline.json)

## 环境

Linux 5.15.0-139-generic x86_64，Git 2.25.1，Python 3.13.13，pytest 8.3.4，Node.js 22.23.1，npm 10.9.8。SQLite 3.51.2，Rasterio GDAL 3.9.3，Pyogrio GDAL 3.11.4，PROJ 9.5.1，GEOS 3.13.1。默认 PATH 未找到 QGIS Processing CLI。Docker 的既有 QGIS 3.44.15 镜像在本机可见，CLI 启动验证留至 P12-05 环境准备。

## 执行与复验

从审计工作树的原型目录运行。原工作树路径用于采集当时的未提交改动。

```bash
git rev-parse HEAD
git status --short --branch
python scripts/quality/build_source_inventory.py \
  --root /home/mtd/liweigis/GeoPilot/system-prototype-v1 \
  --ref 80d724f35fba31cacb9d3f740265b2713c6be6f4
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 PYTHONDONTWRITEBYTECODE=1 python -m pytest \
  -q -p no:cacheprovider tests/quality/test_source_inventory.py \
  --junitxml=docs/round2/audit/p12-01-junit.xml
```

Git 检查、清单生成及原样复跑比较均退出 0。新工具的 3 项测试通过，覆盖工作树修改与删除、带空格和非 ASCII 文件名、非法提交及缺失目录。 [JUnit](p12-01-junit.xml) · [测试输出](p12-01-pytest.log)

新增文件为清单 CLI、专项测试、基线 JSON、本文及测试原始证据。本任务无 API、Schema 或数据库变更。历史测试及证据完整性由 P12-02—P12-07 核查。回滚可撤销本任务提交，历史审计提交仍可从 Git 重建。
