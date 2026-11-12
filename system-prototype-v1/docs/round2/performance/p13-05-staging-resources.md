# P13-05 暂存资源复验

## 矢量子进程的导入开销

M 档三个矢量负载的候选 RSS 观测中位数较高。原候选 `e874a0f` 的矢量写入器导入暂存模块，顶层依赖又经步骤适配器加载 Rasterio。修正版 `a2d2e6874fd7d5d2a239de9d95a927f8505e2ebc` 将父进程所需的依赖延迟至相关函数调用，只改动 `output_staging.py`。暂存、发布与恢复逻辑保持原实现。

使用正式 M 的原生镜像，原版、原候选和修正版对五个入口分别启动三个独立 Python 进程，共 45 次，均退出 0。修正版在原候选只读源码上覆盖该单个文件，覆盖文件与冻结提交逐字节一致。

| 导入或工具入口 | 原版 RSS 中位数（MiB） | 原候选（MiB） | 修正版（MiB） | Rasterio 加载次数（原版 / 原候选 / 修正版） |
| --- | ---: | ---: | ---: | ---: |
| 矢量写入模块 | 119.34 | 138.89 | 119.93 | 0 / 3 / 0 |
| 空间关系工具 | 120.32 | 139.98 | 120.70 | 0 / 3 / 0 |
| 矢量裁剪工具 | 120.15 | 140.47 | 121.48 | 0 / 3 / 0 |
| 面归属统计工具 | 121.51 | 140.87 | 121.37 | 0 / 3 / 0 |
| 最短路径工具 | 141.38 | 142.02 | 141.73 | 3 / 3 / 3 |

修正版前三个矢量工具及写入模块的启动 RSS 回落约 19.0–19.5 MiB。最短路径工具仍通过原有依赖加载 Rasterio，启动 RSS 接近。修正版暂存及真实写入专项 20 项通过。正式方法的资源峰值需要用修正版重新采样。完整原始观测、命令和补丁位于 `artifacts/test-results/p13-05-staging-import-proof.jW2GNE/`。 [导入探针摘要](p13-05-staging-import-probe.jsonl) · [专项 JUnit](p13-05-staging-import-junit.xml)

## 遥感成果发布前的取消和超时

原版 `977ab41` 与修正版 `a2d2e68` 使用同一份四像元输入执行生产 `calculate_change` 工具。同步点验证差值 TIFF 和分类 TIFF 都可读，类别为 1、2、3 和 NoData。随后分别触发真实取消和 8 秒超时。两侧步骤均退出 -15，持久终态分别为 `CANCELLED/NOT_CHECKED` 和 `FAILED/NOT_CHECKED`，错误为 `PROCESS_CANCELLED` 和 `PROCESS_TIMEOUT`。

两种失败下，原版分别残留两个 TIFF，共 3,671 字节。修正版均清除两份文件和暂存目录。四次运行均保留输入指纹和已有文件，无成果发布，进程已回收且租约释放。首次夹具以普通包的方式读取命名空间包路径，提前退出 1。修正路径核验后，四次真实运行及配对核对退出 0。源码固定信息、实际日志、SQLite、文件观测及脚本位于 `artifacts/test-results/p13-05-remote-stage-control.Gguami/`。 [实际配对结果](p13-05-remote-stage-control.json)

## 数据适配和 GIS 回归

从修正版固定源副本执行 `python -B -m pytest -q -p no:cacheprovider tests/data_adapters tests/gis`，Python 3.13.13、Psycopg 3.3.4 和原 P12 固定 PostGIS 镜像。首次单 CPU 低优先级运行通过 360 项，PostGIS 并发刷新在 SQLite `BEGIN IMMEDIATE` 遇到锁，退出 1。原版与修正版的 PostGIS 刷新和 SQLite 连接实现逐字节相同。两侧单独复跑该用例均通过。恢复正常 CPU 调度后，完整套件同样的 361 个用例全部通过，0 错误、0 跳过，退出 0。命令、源摘要及原始输出位于 `artifacts/test-results/p13-05-domain-tests.xmIqZI/`，入库的首次日志和 JUnit 去除行尾空格。 [首次日志](p13-05-domain-tests-initial.log) · [首次 JUnit](p13-05-domain-tests-initial-junit.xml) · [两侧专项复跑](p13-05-domain-postgis-recheck.log) · [完整复跑](p13-05-domain-tests.log) · [JUnit](p13-05-domain-tests-junit.xml)

修正版 M 复测已从 `artifacts/test-results/p13-05-vector-m-revised-plan.qp9bS7/run-m-revised.py` 启动。四个原版分片已经用冻结比较器独立校验并保留，修正版另生成 56 个运行后按输入及环境回执逐组对照。输出根为 `/mnt/datab/geopilot-p13-05-vector-m-revised.a2d2e68`。L 矢量和栅格启动器也已固定同一修正版，分别位于 `artifacts/test-results/p13-05-vector-l-plan.a2d2e68.C905zE/` 与 `artifacts/test-results/p13-05-raster-plan-a2d2e68.TONYtP/`。三者共用正式测量锁。
