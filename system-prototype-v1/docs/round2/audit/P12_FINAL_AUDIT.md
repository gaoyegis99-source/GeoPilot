# P12 深度审计与阶段放行

- 责任人：主控。2026-11-12 签收 P12-12，P12 共 12/12 ACCEPTED，按下述边界放行第二轮后续开发。
- V1 产品测试基线为 `80d724f35fba31cacb9d3f740265b2713c6be6f4`，冻结了 606 个文件。签收输入截至审计提交 `989d293`，P12-01 至 P12-11 全部 ACCEPTED。产品源码、配置、前端及生产 Schema 保持该 V1 基线。 [任务看板](../../../plans-round2/06_PROGRESS/TASK_BOARD.md) · [基线](baseline.md)
- 托管 CI 在独立五文件提交 `02ff664734605bbe05668d06e88778b3fc92e3ad` 首次成功，和审计分支的工作流、校验器及对应测试一致。Python 194、前端 68、原生 QGIS 479，均无失败、错误或跳过。 [CI 验收](p12-08.md) · [托管运行](https://github.com/gaoyegis99-source/GeoPilot/actions/runs/43148595202)

## 同提交测试基线

| 范围 | 本轮独立证据 | 结果 |
|---|---|---|
| 后端全量 | 净 V1 工作树，真实 QGIS 3.44.15、可选 Psycopg 3.3.4 与同摘要 PostGIS 镜像 | 1,670 通过，0 失败、错误、跳过。首轮 1 失败、14 跳过保留。 [报告](p12-05.md) |
| 前端 | 同一 V1 提交，Node 22.23.1、npm 10.9.8 与锁文件安装 | 68 通过，TypeScript 与 Vite 构建通过，保留包体提示。 [报告](p12-06.md) |
| 独立固定用例 | 同一 V1 提交，真实 QGIS，另起 pytest 与运行目录 | 476 通过。82 个映射节点证明 T01–T16 为 16 PASS、0 FAIL、0 NOT_RUN。 [报告](p12-07.md) |
| 追踪、交付及配置 | 历史台账、逐条引用扫描、原始 manifest 差异、配置和 Schema 校验 | 40/40、16/16、122/122 是历史台账对账。旧 manifest 严格复验 FAIL，16 条证据缺失和 37 条源码变化保留。11 份 Schema 与模型一致。 [对账](p12-02.md) · [Manifest](p12-04.md) · [Schema](p12-09.md) |

## AUD-01—12 分类与后续条件

| ID | 优先级 | 归类 | 负责人 | 证据 | 后续与关闭条件 |
|---|---|---|---|---|---|
| AUD-01 | P1 | 已修复 | F | [台账对账及最终报告](p12-02.md) | 40/40、16/16、122/122 已与历史来源对账并注明三轮回归口径。 |
| AUD-02 | P1 | 已修复 | 主控/F | [后端](p12-05.md) · [前端](p12-06.md) · [固定用例](p12-07.md) | V1 同一提交的三份 JUnit 已核对。历史运行数量保留。 |
| AUD-03 | P1 | 设计边界 | F/A | [引用逐条分类](p12-03-review.md) · [交付清单差异](p12-04.md) | 自动扫描已完成。823 处历史运行引用的目标未入库，旧清单不能直接复验；P16-06 要求新交付材料能按同版本验证。 |
| AUD-04 | P1 | 已修复 | F/主控 | [首次托管三层 CI](p12-08.md) | 原生 QGIS、Python 与前端三项作业已通过，工作流纳入审计分支。 |
| AUD-05 | P1 | 未验证 | B/A/F | [规模与资源协议](p12-11.md) | P13-01 至 P13-04 生成三档并输出重复、可校验的真实测量。 |
| AUD-06 | P1 | 真实缺陷 | D/F | [锁等待与调度线程退出复现](concurrency-model.md) | 保持单活动。P13-07 必须证明写锁超时恢复或明确降级、队列可继续、无重复领取和无孤儿，再签署并发 ADR。 |
| AUD-07 | P2 | 设计边界 | G/主控 | [V1 模型基线与边界](../../../plans-round2/01_AUDIT/P12_DEEP_AUDIT_REPORT.md) · [固定用例 T15](p12-07.md) | P15-07 交付第二供应商适配决策与契约测试，验证失败回退和数据外发边界。 |
| AUD-08 | P2 | 设计边界 | C/B/F | [V1 方法目录与人工流程](../../../plans-round2/01_AUDIT/P12_DEEP_AUDIT_REPORT.md) · [配置校验](p12-09.md) | P14 验证方法从模板、静态检查、隔离试跑、人工审批到回滚的全流程及复杂试点。 |
| AUD-09 | P2 | 暂缓 | A/B | [本机 QGIS 环境](qgis-preflight.md) · [托管原生版本](p12-08.md) | P16-01/02 补齐 doctor 与干净环境矩阵，明确已验证平台及可复现安装。 |
| AUD-10 | P2 | 暂缓 | A/F | [锁定依赖及托管执行](p12-08.md) | 当前有精确依赖、安装与 pip check。P16 形成依赖兼容性与受控升级报告。 |
| AUD-11 | P2 | 未验证 | E/F | [真实用户证据缺口](../../../plans-round2/01_AUDIT/P12_DEEP_AUDIT_REPORT.md) | P16-03 采集真实 GIS 用户任务记录、完成率、求助、错误与耗时。 |
| AUD-12 | P3 | 已修复 | C/F | [配置及 Schema 本地与 CI 证据](p12-09.md) | 静态防漂移门禁已运行。P14 接入方法开发、版本兼容及发布校验。 |

## 放行范围与风险

P13、P14 按各自依赖并行。P15 等待执行反馈和方法生态接口稳定。P16 环境诊断与用户研究可提前准备，发布需真实用户和回归证据。P13 维持当前单活动调度，写锁存活问题及首次复跑的 QGIS、进程启动耗时波动进入遥测与压力验证。GIS 计算继续按 CRS、测量算法、栅格网格与 NoData、波段与尺度规则验证。所有模型建议仍走确定性校验、用户确认和版本化追溯。 [阻塞与风险](../../../plans-round2/06_PROGRESS/BLOCKERS.md)

本任务新增阶段门禁校验器、专项测试、本报告和原始证据。没有修改运行接口、数据库或算法。撤销 P12-12 审计提交可回滚阶段签收记录，原始 V1 测试与既有证据保持可追溯。

## P12-12 实际验收命令

在审计提交 `989d293` 的工作树上增加门禁与本报告后执行。Linux 5.15.0-139-generic x86_64、Python 3.13.13、pytest 8.3.4。

```bash
set -o pipefail
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python scripts/quality/validate_round2_status.py
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest \
  -q -p no:cacheprovider tests/quality tests/benchmarks \
  --junitxml=docs/round2/audit/p12-12-junit.xml 2>&1 | tee docs/round2/audit/p12-12-pytest.log
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python scripts/quality/validate_round2_status.py --require-released
git diff --exit-code 80d724f35fba31cacb9d3f740265b2713c6be6f4 -- src config frontend artifacts/schemas
```

门禁预检退出 0，核对 11 项前置任务、12 条 AUD、同提交 JUnit、固定用例哈希及托管作业元数据。首次工具回归退出 1，72 通过、1 失败，原因是新增测试夹具仍匹配旧看板负责人字段。修正夹具后同样的 73 项全部通过，0 失败、0 错误、0 跳过，退出 0，21.35 秒。严格放行复验退出 0。生产范围 Git 对比退出 0。 [预检](p12-12-ready.json) · [首次 JUnit](p12-12-first-junit.xml) · [首次日志](p12-12-first-pytest.log) · [复跑 JUnit](p12-12-junit.xml) · [复跑日志](p12-12-pytest.log) · [严格放行复验](p12-12-gate.json)
