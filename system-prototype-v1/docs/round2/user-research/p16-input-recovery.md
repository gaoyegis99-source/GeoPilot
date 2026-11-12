# P16-04 输入变更后的重新登记

主控、E、F 在 `2a34e96c39fd15b13eea32b8b4a38aaea99e60b2` 上验收。启动确认方案时，`409 INPUT_CHANGED` 在运行区显示输入阶段和中文恢复步骤。「打开资产登记」打开原有表单并聚焦路径框。操作者手动登记对应文件与图层、改选新版本并生成草稿。技术详情保留原错误码、字段和来源提示。

| 文件 | 改动 |
| --- | --- |
| `frontend/src/App.tsx` | 将启动时的 `INPUT_CHANGED` 归入输入阶段，并连接资产登记入口。 |
| `frontend/src/components/errors/ProblemDetails.tsx` | 提供中文恢复步骤，保留原始技术诊断。 |
| `frontend/src/features/assets/AssetPanel.tsx` | 打开已有登记表单并聚焦路径输入。 |
| `frontend/src/App.input-recovery.test.tsx`、`frontend/src/components/errors/ProblemDetails.test.tsx` | 验证手动登记与版本选择、原方案保留及成果变更的重查路径。 |

F 在 Chromium 141.0.7390.37 和 QGIS 3.44.15 上对照旧版 `fe10a91` 与修复版。旧方案首次计算成功后，在临时道路 GeoPackage 中修改一处属性。再次启动旧方案收到 409，数据库只保留旧运行。浏览器从新入口登记同一资产的 v2，选择 v2 并生成、确认同一方案的 v2 后，新运行执行和检查均通过。独立复核确认两次计算的固化道路副本各含相应版本的属性。旧运行记录、步骤、报告、成果及 33 份文件保持一致。浏览器读取旧运行的步骤日志与完整日志均返回 200，并从历史重新下载与原件一致的 CSV。西区 3、东区 2、未归属 1。

旧版复现和修复版最终浏览器验收均退出 0。修复版初次脚本将 JSON 键排序误当运行顺序，退出 1。修正测试断言后，同一源码独立复跑通过。前端专项 17 项、全量 78 项、TypeScript 及生产构建通过，后端相关 24 项通过。命令、环境、日志、退出码、JUnit、浏览器轨迹和独立只读审计见归档。SHA-256 为 `27e982ef0b9f98bb372bef0561e823c0328006a2582d45f4c3008b204cf3af4c`，解包 94 个文件校验通过。 [原始证据](p16-input-recovery-evidence.tar.gz)

结合草稿 CRS 定位、计算取消后重跑及检查取消后重查证据，P16-04 技术验收为 ACCEPTED。接口与状态契约保持原定义。可撤销源码提交 `2a34e96` 和验收记录回滚此交互。 [草稿与重跑](p16-ux-follow-up.md) · [重查](p16-check-recovery.md)
