# 最终验收清单

2026-11-12 主控逐项复核。[最终报告](../../docs/release/FINAL_ACCEPTANCE.md)

- [x] 40 条通用需求均映射到实现与测试，部分能力明确标记。[矩阵](../../docs/traceability/requirements-matrix.md)
- [x] T01—T16 全量记录 PASS/FAIL/NOT_RUN，数量合计 16。[复跑](../../docs/acceptance/P11-02.md)
- [x] 道路缓冲任务真实闭环可复算。[双方法报告](../../docs/acceptance/P11-05.md)
- [x] 至少一个不同类型方法复用同一方案/执行/成果链。[属性筛选](../../docs/acceptance/P11-05.md)
- [x] 执行状态和检查状态独立。[接口审计](../../docs/acceptance/interface-audit.md)
- [x] 取消后子进程停止；失败后无后续步骤派发。[故障演练](../../docs/acceptance/lifecycle.md)
- [x] 重试生成新 run 目录；原始输入不变。[故障演练](../../docs/acceptance/lifecycle.md)
- [x] 成果能追溯到输入资产版本、方案版本、工具版本和运行记录。[复现包](../../artifacts/reproduction/p11-05/README.md)
- [x] 外部模型请求通过 T15，且模型不可用时手工入口可用。[模型验收](../../docs/acceptance/P9.md)
- [x] MapLibre 预览与原始栅格数值分离。[真实浏览器原像元](../../artifacts/smoke/p8-10/browser.7TgLX6/browser.json)
- [x] 新工作目录按启动文档可运行。[净目录验收](../../docs/acceptance/P11-07.md)
- [x] 未完成能力、可选依赖和限制如实披露。[已知限制](../../docs/release/FINAL_ACCEPTANCE.md#已知限制)
