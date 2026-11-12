# 依赖状态登记

主控每次派发前更新。只有依赖任务状态为 ACCEPTED 才算满足。

| 阶段 | 前置 | 当前放行状态 | 证据 |
|---|---|---|---|
| P0 环境与参考样本 | 现有设计文档；本机 GIS 环境 | ACCEPTED | [P0 验收](../../docs/acceptance/P0.md)，7/7 任务通过，15 项集成测试通过 |
| P1 数据与状态存储 | P0 | ACCEPTED | [P1 验收](../../docs/acceptance/P1.md)，10/10 任务通过，联合回归 164 项 |
| P2 契约与基础测量 | P1 | ACCEPTED | [P2 验收](../../docs/acceptance/P2.md)，10/10 任务、380 项回归和 5 个固定用例通过 |
| P3 通用方法执行链 | P2 | ACCEPTED | [P3 阶段验收](../../docs/acceptance/P3.md)，10/10 任务、626 项累计回归，T07/T08 PASS |
| P4 运行管理与接口 | P3 | ACCEPTED | 16/16，T16 PASS，真实故障、取消、重启与重查通过。[阶段验收](../../docs/acceptance/P4.md) |
| P5 通用 Web 工作台 | P4 | ACCEPTED | 12/12，两种方法和刷新实际验证。[阶段报告](../../docs/acceptance/P5.md) |
| P6 矢量能力补齐 | P3；页面联调依赖 P5 | ACCEPTED | 11/11，T04、T13、T14 和正式土地利用样例通过，真实工作台已联调。[阶段验收](../../docs/acceptance/P6.md) |
| P7 DEM 与栅格预览 | P4、P5 | ACCEPTED | 12/12，T09 PASS，正式七步、预览和独立原像元验证通过。[阶段报告](../../docs/acceptance/P7.md) |
| P8 遥感变化 | P7 | ACCEPTED | 10/10、T10–T12 PASS，原生打包、八步和浏览器通过，阶段联合 234。[阶段验收](../../docs/acceptance/P8.md) |
| P9 模型入口 | P5 与已可用方法 | ACCEPTED | 9/9，T15 PASS，真实模型与手工执行等价。[阶段报告](../../docs/acceptance/P9.md) |
| P10 脚本与可选数据源 | P4；收录使用已接入工具 | ACCEPTED | 7/7，真实脚本隔离和 PostGIS 版本离线复现。[阶段报告](../../docs/acceptance/P10.md) |
| P11 原型与方法验收 | 共用基础及本轮选定方法完成 | ACCEPTED | 8/8 签收、122/122 原子任务通过，[P11 阶段](../../docs/acceptance/P11.md) · [最终报告](../../docs/release/FINAL_ACCEPTANCE.md) |
