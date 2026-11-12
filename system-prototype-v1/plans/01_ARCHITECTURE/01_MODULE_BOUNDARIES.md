# 模块边界与调用方向

## 建议逻辑分层
1. Frontend：Web GIS 工作台、MapLibre、动态表单、运行/成果视图。
2. Application：任务、上下文、方案、确认、运行服务和 API。
3. Domain：DataAsset、MethodDefinition、ToolContract、AnalysisPlan、Run、Artifact、CheckReport。
4. GIS services：CRS/单位/测量策略、矢量/栅格/遥感算法和规则。
5. Execution：调度器、QGIS runner、Python runner、进程管理。
6. Persistence：SQLite repositories、schema/migrations、workspace file references。
7. Model adapter：外部模型请求、允许摘要、响应解析与候选校验。

## 单向依赖
Frontend → API/Application → Domain + Services → Execution/Persistence adapters。
GIS 子进程不直接写 SQLite；只通过请求文件、输出清单、退出码和日志交接。
模型适配器不得直接调用 GIS 计算，不得直接确认方案或创建运行。

## 路径映射规则
以上为逻辑职责，不是强制目录。P0-01 必须先识别现有结构；优先扩展现有模块，避免平行造轮子。实际映射写入 `01_ARCHITECTURE/05_REPOSITORY_PATH_MAP.md`。
