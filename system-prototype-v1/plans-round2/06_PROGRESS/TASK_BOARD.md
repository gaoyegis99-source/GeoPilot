# 第二轮任务看板

| Task | 阶段 | 名称 | Agent | 依赖 | 状态 | 证据 |
|---|---|---|---|---|---|---|
| P12-01 | P12 | 固定审计基线与仓库快照 | 主控 | 无 | ACCEPTED | [基线、命令与测试](../../docs/round2/audit/baseline.md) |
| P12-02 | P12 | 需求矩阵与最终报告统计对账 | F | P12-01 | ACCEPTED | [对账及历史统计](../../docs/round2/audit/p12-02.md) |
| P12-03 | P12 | 文档与证据链接完整性扫描 | F | P12-01 | ACCEPTED | [扫描与人工复核](../../docs/round2/audit/p12-03-review.md) |
| P12-04 | P12 | 交付 Manifest 完整性校验 | A/F | P12-01、P12-03 | ACCEPTED | [严格校验与逐文件差异](../../docs/round2/audit/p12-04.md) |
| P12-05 | P12 | 后端全量回归复跑 | 主控/F | P12-01、P12-02；QGIS CLI 可用 | ACCEPTED | [同提交 1,670 项全部通过](../../docs/round2/audit/p12-05.md) |
| P12-06 | P12 | 前端测试与构建基线 | E/F | P12-01 | ACCEPTED | [同提交前端测试与构建](../../docs/round2/audit/p12-06.md) |
| P12-07 | P12 | T01—T16 固定用例独立复跑 | B/F | P12-05；QGIS CLI 可用 | ACCEPTED | [独立 476 项与 T01–T16 映射](../../docs/round2/audit/p12-07.md) |
| P12-08 | P12 | GitHub Actions 分层 CI | F/主控 | P12-05、P12-06；依赖策略冻结 | ACCEPTED | [首次托管三项作业通过](../../docs/round2/audit/p12-08.md) |
| P12-09 | P12 | 配置与 Schema 漂移检查 | C/F | P12-01 | ACCEPTED | [本地回归及托管校验均通过](../../docs/round2/audit/p12-09.md) |
| P12-10 | P12 | 调度单活动约束与并发安全审计 | D/F | P12-01 | ACCEPTED | [并发模型、专项回归与缺陷复现](../../docs/round2/audit/concurrency-model.md) |
| P12-11 | P12 | 数据规模与性能基准方案 | B/F/A | P12-01、P12-05 | ACCEPTED | [三档方案、样本探测与 13 项测试](../../docs/round2/audit/p12-11.md) |
| P12-12 | P12 | P12 审计签收与第二轮放行 | 主控 | P12-01 至 P12-11 | ACCEPTED | [最终审计与放行证据](../../docs/round2/audit/P12_FINAL_AUDIT.md) |
| P13-01 | P13 | GIS 数据规模分级生成器 | B/A/F | P12-11 | ACCEPTED | [S/M/L 实际生成、独立参考和联合复验](../../docs/round2/performance/p13-01.md) |
| P13-02 | P13 | 执行资源遥测 | D/F | P12-10、P13-01 | ACCEPTED | [旁路采样、真实运行及 T01–T16 复验](../../docs/round2/performance/p13-02.md) |
| P13-03 | P13 | 矢量处理性能基线 | B/F | P13-01、P13-02 | ACCEPTED | S/M/L 各完成 20 个冷热组。S/M 正式样本各 100 通过，L 140 次实际失败，保留失败分位及资源观测。 [基准报告](../../docs/round2/performance/p13-03.md) · [独立复算](../../docs/round2/performance/p13-03-L-final-audit.log) |
| P13-04 | P13 | 栅格与遥感性能基线 | B/F | P13-01、P13-02 | ACCEPTED | 工具 84 通过，完整方法 42 通过、42 超时。 [基准与原始证据](../../docs/round2/performance/p13-04.md) |
| P13-05 | P13 | 大数据内存与临时文件控制 | A/B | P13-03、P13-04 | DOING | 遥感 L 正式配对完成，原版 14 轮超时，修正版 14 轮 GIS 与验算通过，5 次正式计量有状态观察失败。T14 局部裁回候选运行中，之后为 DEM M 原版重试及容量探针。 [配对资源复算](../../docs/round2/performance/p13-05-resource-coverage.md) · [矢量 M](../../docs/round2/performance/p13-05-vector-m-revised.md) · [矢量 L](../../docs/round2/performance/p13-05-vector-l.md) · [栅格 S](../../docs/round2/performance/p13-05-raster-s.md) · [栅格 M/L](../../docs/round2/performance/p13-05-raster-m-l.md) · [暂存复验](../../docs/round2/performance/p13-05-staging-resources.md) |
| P13-06 | P13 | 中间成果缓存与失效策略 | A/D | P13-02、P13-05 | BLOCKED | 已核对执行绑定和图层身份，先后重算 224 个正式栅格成果和遥感 L 的 140 个文件组指纹，并验证辅助文件改变指纹。等待 P13-05 验收。 [复用证据准备](../../docs/round2/performance/p13-06-preflight.md) |
| P13-07 | P13 | 运行队列与取消压力测试 | 主控/D/F | P12-10、P13-02 | ACCEPTED | SQLite 锁恢复、队列取消与重启已验证。正常并发探针共 724 次状态 GET 成功，20 次实际提交锁窗口已复算，冻结批底层原因仍待记录。 [验收](../../docs/round2/performance/p13-07.md) |
| P13-08 | P13 | 并发执行可行性决策 | 主控/D/F | P13-02 至 P13-07 | BLOCKED | 已验证外部容器在本地租约释放后仍可运行，待多档基准、内存与缓存验收。 [生命周期证据](../../docs/round2/decisions/p13-08-container-lifecycle-preflight.md) |
| P14-01 | P14 | 方法开发者 CLI 需求与接口设计 | C/F | P12-09 | ACCEPTED | [CLI 契约、实测和只读守卫](../../docs/round2/methods/p14-01.md) |
| P14-02 | P14 | 方法配置模板生成 | C | P14-01 | ACCEPTED | [独立 Schema 核验与联合回归](../../docs/round2/methods/p14-02.md) |
| P14-03 | P14 | 方法配置静态 lint | C/F | P14-01、P12-09 | ACCEPTED | [目录、失败路径、原 CLI 回归和复验](../../docs/round2/methods/p14-03.md) |
| P14-04 | P14 | 方法样例包与参考输出协议 | B/C/F | P14-01 | ACCEPTED | [真实 V1 双样例、独立参考和失败路径](../../docs/round2/methods/p14-04.md) |
| P14-05 | P14 | 方法隔离试运行与验证报告 | B/C | P14-02 至 P14-04 | ACCEPTED | [独立试跑、正式成果隔离和进程回收](../../docs/round2/methods/p14-05.md) |
| P14-06 | P14 | 方法版本兼容与弃用策略 | 主控/C | P14-03、P14-04 | ACCEPTED | [精确版本检查、旧计划重放及迁移规则](../../docs/round2/methods/p14-06.md) |
| P14-07 | P14 | 方法审批发布与回滚 | C/主控 | P14-05、P14-06 | ACCEPTED | [审批台账、隔离发布和回滚复跑](../../docs/round2/methods/p14-07.md) |
| P14-08 | P14 | 第二种复杂方法扩展试点 | C/主控 | P14-01 至 P14-07；产品决策选定场景 | ACCEPTED | [路网最短路径、独立参考及 Chromium](../../docs/round2/methods/p14-08.md) |
| P15-01 | P15 | Agent 任务澄清状态模型 | 主控/G/C | P12-10、P14-03 | ACCEPTED | [版本化报告、Schema 与确认守卫](../../docs/round2/agent/p15-01.md) |
| P15-02 | P15 | 多轮任务上下文与草稿修订 | 主控/G/C/E | P15-01 | ACCEPTED | [逐项回答、历史保护与刷新恢复](../../docs/round2/agent/p15-02.md) |
| P15-03 | P15 | 执行反馈结构化解释 | 主控/G/D | P13-02、P15-01 | ACCEPTED | [本地反馈接口、事实与推断、脱敏提示](../../docs/round2/agent/p15-03.md) |
| P15-04 | P15 | 受约束的替代方案建议 | 主控/G/C | P14-03、P15-03 | ACCEPTED | [已登记候选对照与显式草稿选择](../../docs/round2/agent/p15-04.md) |
| P15-05 | P15 | 失败后的计划修订闭环 | 主控/D/G | P15-02 至 P15-04 | ACCEPTED | [真实失败、方案修订和成功新运行](../../docs/round2/agent/p15-05.md) |
| P15-06 | P15 | 代码型 GIS 工具生成隔离试点 | C/主控 | P14-04、P14-05、P15-03 | ACCEPTED | [固定 GeoTIFF、候选生成和真实容器限制](../../docs/round2/agent/p15-06.md) |
| P15-07 | P15 | 第二模型供应商适配决策 | 主控/G | P12-07、P15-01 | ACCEPTED | [本机兼容端点与契约复验](../../docs/round2/agent/p15-07.md) |
| P15-08 | P15 | Agent 行为评估集 | F/G/主控 | P15-01 至 P15-07 | ACCEPTED | 固定行为 18/18，本机建议严格 0/3、越权护栏 1/1。 [评估结果](../../docs/round2/agent/p15-08.md) |
| P16-01 | P16 | 本机环境诊断命令 | 主控 | P12-05、P12-06 | ACCEPTED | [诊断 CLI、真实环境和失败路径](../../docs/round2/deployment/p16-01.md) |
| P16-02 | P16 | 干净环境安装矩阵 | 主控/A/E/F | P16-01 | ACCEPTED | [两套 Linux 后端与独立 Chromium 实测](../../docs/round2/deployment/p16-02.md) |
| P16-03 | P16 | 工作台用户任务可用性测试 | E/F | P12-12、至少一个稳定方法 | DOING | 主持准备 GIS 后端，记录校验允许浏览器端 OS 按需填写。待真实浏览器会话、匿名记录和下载 CSV。 [主持手册](../../docs/round2/user-research/moderator.md) |
| P16-04 | P16 | 错误信息与恢复路径优化 | E/D | P16-03 | BLOCKED | 待 P16-03 真实观察。 |
| P16-05 | P16 | 长任务用户体验 | E/D | P13-02、P16-03 | BLOCKED | 待 P16-03 真实观察。 |
| P16-06 | P16 | 交付包与复现材料整理 | 主控/F/A | P12-04 至 P12-08、已选 P13/P14 | BLOCKED | 原生镜像离线恢复、证据链接及组合预演收集通过，2,178 个后端节点包含全部 82 个固定节点。正式包待 P13 及最终同提交证据。 [打包验证](../../docs/round2/deployment/p16-06-tooling.md) |
| P16-07 | P16 | V2 产品范围与发布决策 | 主控 | P13-08、P14-08、P15-08、P16-01 至 P16-06 | BLOCKED | 待 P13-08 及 P16-03 至 P16-06 验收。 |
