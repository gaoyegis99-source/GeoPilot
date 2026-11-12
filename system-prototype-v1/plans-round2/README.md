# GeoPilot P12 深度审计与第二轮多智能体开发计划

基于 `gaoyegis99-source/GeoPilot` 的 `main` 分支、`system-prototype-v1` 源码、配置、任务看板、验收文档和测试证据形成。

## 使用顺序
1. 在本地克隆仓库并确认当前 commit。
2. 将本目录放入 `system-prototype-v1/plans-round2/`，不要覆盖原 `plans/`。
3. Codex 读取 `00_GOVERNANCE/01_CODEX_START_PROMPT.md`。
4. 先执行 P12-01 至 P12-04，复核静态审计发现。
5. 再执行 P12-05 至 P12-11，建立统一复跑、CI 和性能基线。
6. P12-12 签收后，才放行 P13—P16。

本计划包含 P12—P16 五阶段、原子任务独立 Codex 指令、文件范围、依赖、命令和验收标准。P13—P16 是基于审计提出的规划，不表示这些功能已经实现。

## 审计边界
本次基于 GitHub 可见仓库静态审查，没有在用户本机执行代码。历史验收文档中的测试结果属于仓库记录，不等于本次独立复跑。所有状态必须在本地当前 commit 上重新核实。
