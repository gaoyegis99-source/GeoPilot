# P12-03 文档与证据链接审计

- 执行：Agent F（证据链接）。主控复核归类和专项测试后签收，状态 ACCEPTED。
- 审计提交：`80d724f35fba31cacb9d3f740265b2713c6be6f4`。工具开发基于 `c12898f`。Linux x86_64，Git 2.25.1，Python 3.13.13，pytest 8.3.4。
- 新增扫描器 `scripts/quality/check_doc_evidence_links.py`、专项测试 `tests/quality/test_doc_evidence_links.py` 以及本目录的扫描、复核和原始测试证据。

## 结果

固定提交中扫描 315 份 Markdown、3,257 处引用。计数为引用次数，同一目标可被不同文档或命令多次引用。

| 分类 | 引用数 | 人工复核 |
|---|---:|---|
| `tracked` | 1,989 | 979 处 Markdown 链接和 1,010 处裸路径指向提交中的文件或目录。 |
| `anchor` | 143 | 标题及代码行锚点可定位。 |
| `external` | 67 | 外部链接单列。可达性未测试。 |
| `untracked_run_output` | 823 | 512 个目标路径，641 处是 Markdown 链接。按输出根统计：473 处 test-results、334 处 smoke、12 处 reproduction、4 处本机 QGIS 入口。 |
| `machine_local` | 87 | 82 个主机或临时目录路径，需原机器或新一轮复跑。 |
| `removed_from_tree` | 17 | 15 处 Markdown 链接、2 处裸路径，集中于设计 PDF 和 Markdown 源文档两个目标。两者均在该提交移除，Git 历史可定位。 |
| `template_path` | 120 | 原子任务逻辑路径、通配符和运行目录占位路径需展开。 |
| `missing` | 11 | 10 个裸路径，逐项归类如下。 |

CSV 包含来源、行号、原引用、分类、解析目标和备注。显式 Markdown 链接按源文档目录解析。命令和说明中的裸路径按扫描范围、前端目录及来源目录寻找已知目标，相对父目录路径优先按来源目录检查。范围为指定 Git 树内的 Markdown 文件，证据解析使用整个 Git 树。扫描覆盖单行 Markdown 链接、图片、链接定义和具有项目或机器路径前缀的裸路径。引擎按 Git 历史识别重命名，未用同名文件推断移动。本轮实际没有 Git 已确认的移动，专项测试验证了该分支。 [完整 CSV](evidence-link-audit.csv) · [扫描输出](p12-03-scan.log)

逐条比较 P12-02 原始追踪检查诊断和 CSV 中需求矩阵的 Markdown 目标。两边的路径及出现次数完全相等，均为 63 处引用、45 个不同目标，全部归为 `untracked_run_output`。需求矩阵另有一处设计 Markdown 链接归为 `removed_from_tree`。 [P12-02 追踪检查原始日志](p12-02-v1-traceability.log) · [计数对比](p12-03-traceability.log)

### 缺失裸路径复核

| 路径 | 次数 | 语境及复验依据 |
|---|---:|---|
| `./MapPanel`、`./ChecksPanel` | 2 | P5-08 和 P5-10 的历史红测模块导入说明。同段现有测试链接可定位。 |
| `docs/tools` | 1 | V1 初始盘点中的设计报告构建工具。Git 根目录原工具位于审计提交的父提交。 |
| `plans/confirm`、`plans/runs` | 2 | V1 公共契约中的 API 意图。实际路由由源码和接口审计确认。 |
| `artifacts/reports/golden.xml` | 1 | P11-02 任务文档中的预定 JUnit 输出名。该轮纳入 Git 的报告为 `artifacts/test-results/p11-02-fixed-regression-junit.xml`。 |
| `tests/integration/lifecycle/`、`artifacts/reports/lifecycle/` | 3 | P11-06 的逻辑文件范围及预设命令。实际生命周期回归入口见该轮报告。 |
| `frontend/src/features/plans/dynamic-form/` | 1 | P5-04 的预设逻辑文件范围。实际入口位于 `frontend/src/features/method-form/`。 |
| `tests/remote_sensing/test_classification.py` | 1 | P8-09 的预设命令。提交中实际测试文件为 `tests/remote_sensing/test_change_classification.py`。 |

缺失历史运行输出无法从审计提交公开复验。后端可按交付说明配置实际 QGIS 后执行 `python -m pytest -q --junitxml=artifacts/test-results/p12-backend.xml`，并单独执行 `tests/golden` 和 `tests/e2e/test_workbench_reproduction.py`。前端可按 `npm ci`、`npm test -- --run` 和 `npm run build` 复跑。原 Chromium 截图及机器临时路径的具体证据需要新建隔离工作台运行，属于独立证据。设计资料可用 `git show 80d724f^:<Git 根路径>` 查阅原提交内容。 [工作台复跑环境](../../../README.md) · [历史固定用例](../../acceptance/P11-02.md) · [历史生命周期](../../acceptance/lifecycle.md)

## 实际命令与退出码

在原型目录执行：

```bash
python scripts/quality/check_doc_evidence_links.py --root . \
  --ref 80d724f35fba31cacb9d3f740265b2713c6be6f4 \
  --report docs/round2/audit/evidence-link-audit.csv
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 PYTHONDONTWRITEBYTECODE=1 python -m pytest \
  -q -p no:cacheprovider tests/quality/test_doc_evidence_links.py \
  --junitxml=docs/round2/audit/p12-03-junit.xml
set -o pipefail
python scripts/quality/check_doc_evidence_links.py --root . \
  --ref 80d724f35fba31cacb9d3f740265b2713c6be6f4 \
  --report /dev/fd/3 3>&1 1>/dev/null | cmp docs/round2/audit/evidence-link-audit.csv -
```

扫描、专项测试、逐字节复跑对比均退出 0。专项测试 3 通过、0 失败、0 跳过，覆盖正常输入、未知提交和不存在的目录、空文档范围。CSV SHA-256 为 `b874876bdbecf501e4feeb50a19e787dc6eeeba9ee8dc7dbe27c729baad5504a`。 [JUnit](p12-03-junit.xml) · [测试输出](p12-03-pytest.log)

退出 0 表示分类扫描成功，历史证据缺口见报告。本任务未改动 API、Schema、数据库和历史引用。回滚可撤销本任务提交，扫描结果可从固定提交重建。
