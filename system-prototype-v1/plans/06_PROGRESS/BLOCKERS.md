# 阻塞问题

| ID | 发现任务 | 问题/复现 | 影响 | 建议方案 | 决策 | 状态 |
|---|---|---|---|---|---|---|
| ENV-01 | P0-01 | `qgis_process --version` 报命令不存在；缓存镜像探测也无 CLI | P0-03、P0 退出、P1 放行 | 隔离安装 QGIS 3.44 或使用包含 CLI 的本机容器 | 官方包与缓存 QGIS 库匹配，真实 CLI 算法与输出通过 [P0 验收](../../docs/acceptance/P0.md) | RESOLVED |
| ENV-02 | P0-06 | pytest 默认临时根的旧目录触发其他项目的清理权限警告 | 本机共享 pytest 执行 | 使用项目专用的默认临时根 | `tests/conftest.py` 隔离到 `artifacts/test-results/tmp/`，实跑通过 | RESOLVED |
| GIS-01 | P0-03 | `native:buffer` 使用平面几何，现有测地线入口只提供基础操作 | P6-09、T04 | 独立实现并验证侧边、端点、转角与拓扑拼合 | [P6-09](../../docs/acceptance/P6-09.md) 通过专用工具的独立几何参考及统一入口验证 | RESOLVED |
| DATA-01 | P1-03/P1-04 交叉审查 | 特定本机文件名可能被 GDAL 路径解析为 URI 或归档 | 本机数据读取 | 先规范为绝对路径，拒绝虚拟路径 | 矢量路径 8 项复验通过，GeoPackage 复用守卫，栅格同步核对 | RESOLVED |
| DATA-02 | P1-07 联合复核 | PAM 原地改写在文件系统时间粒度内可与原状态字段相同 | 元数据与内容一致性 | 在元数据读取前后核对真实内容摘要 | 双摘要修复后，真实字节变化、相同状态观测时返回 INPUT_CHANGED 且零记录，重开复用 | RESOLVED |
| GIS-02 | P2-08 联合复核 | 将真实 QGIS 缓冲绑定 `DISTANCE` 改为 `DISTANCE_TYPO`，QGIS 退出 0，按默认 10 m 计算 500 m 请求 | P2-08、P2-10、P2 出口 | 执行前核对当前 QGIS 算法声明的参数名 | 真实绑定复验与 17 项专项回归通过，[记录](../../docs/acceptance/P2-08.md) | RESOLVED |
| DATA-03 | P3 输入暂存复核 | GDAL 重开含已提交 WAL 的 GeoPackage 时实测触发检查点，物理指纹变化，P1 登记返回 INPUT_CHANGED | 含活动 WAL 的外部源 | 使用已完成检查点的输入或后续数据库快照适配 | 已登记源的暂存复核 26 项通过，直接复制文件组保留已提交 WAL 内容 | TRACKED |
| UI-01 | P11-04 | 同时绘制两层时，MapLibre 图层已出现但 React effect 尚未登记，点击可能查询下层原像元 | 原像元与地图顺序一致性 | 同步登记画布图层和查询目标 | 仅改栅格预览组件，66 项前端回归、五次同用例与 Chromium 五个像元全部通过 | RESOLVED |
