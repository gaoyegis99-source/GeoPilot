# P16-06 交付清单与离线校验准备

主控完成打包机制和专项复验。正式候选包待选定的 P13/P14 阶段证据、最终源提交和同提交验证回执齐备后生成。首次工具验证基于 `51f175fcf03aaf02f8d8580109eb5d50700a74d6` 加本项文件。环境为 Linux x86_64、Python 3.13.13、pytest 8.3.4。

- 候选清单绑定源提交与证据提交，校验两者源文件字节相同。纳入配置、依赖锁、测试、样本、CI、基准配方、选定证据及复现文档。
- 逐用例读取 JUnit，核对计数、实际退出码、同源提交日志和固定 GIS 无跳过。复现文档的本地链接和 Markdown 锚点须有提交对象。
- 归档可按同一输入重复生成相同字节，文件成员取自 Git 提交。离线校验重读解包后的文件指纹和三组 JUnit。历史 V1 清单继续使用原入口。 [生成、校验和离线复现](../../release/ROUND2_REPRODUCTION.md)

本轮 39 项通过、0 失败、0 跳过，退出 0。真实 Git 夹具覆盖两个提交、源码漂移、错误日志、错误 JUnit 计数、缺失链接、归档重复生成、不覆盖现有归档、解包后无 Git 校验以及篡改检测。第二轮实际候选包尚未生成。 [日志](p16-06-tooling-pytest.log) · [JUnit](p16-06-tooling-junit.xml) · [CI 静态核对](p16-06-tooling-ci.json)

补充复验基于 `ddf4e6074b265626690aef3986d013776acb5bec` 和本项变更。生成及离线核验逐一检查固定 GIS JUnit 中的 T01–T16 映射节点。缺失或重复节点时拒绝归档。按同一测试文件复跑 41 项通过、0 失败、0 跳过，退出 0。历史 JUnit 中的 476 个节点包含映射要求的 82 个唯一节点，逐项匹配。正式候选仍待同源提交复验。 [日志](p16-06-coverage-pytest.log) · [JUnit](p16-06-coverage-junit.xml)

验证入口复验基于 `41f2c3e6ff534ae4e5a495c6a952dccc3ce1bd21` 和本项变更。真实 Git 夹具从独立的精确提交调用模拟 Python、QGIS 和 npm，生成回执后再提交证据并生成清单。校验会拒绝命令失配、末行失败、后端 JUnit 与完整收集不符、回归测试材料变动及工作树 QGIS 冲突。首次夹具脚本首行语法错误，退出 1，3 项失败。修正后复跑 50 项通过，0 失败、0 错误、0 跳过，退出 0。历史 V1 后端收集的 1,670 个唯一节点与其 JUnit 一致，用于验证节点解析。模拟执行覆盖验证入口，正式后端、前端和 GIS 复验等待最终源提交。 [首次日志](p16-06-runner-initial.log) · [首次 JUnit](p16-06-runner-initial-junit.xml) · [复跑日志](p16-06-runner-pytest.log) · [复跑 JUnit](p16-06-runner-junit.xml)

证据链接复验基于 `37cc741ed7446e617416156737b8212a48e5e7e9` 加本项变更。F 与主控分别复现验收 Markdown 链接的报告和原始 JSON 缺包，而原生成和离线校验均退出 0。候选递归收录两层链接，用 P12-03 的固定清单逐条识别历史缺链。离线按回执复查，新证据条目与文件同时被移除后校验退出 1。跨文件锚点、空链接和越界路径也由回归覆盖。主控联合四个质量套件，60 项通过，0 失败、0 错误、0 跳过，退出 0。清单格式与 CLI 参数沿用原契约。回滚可撤销此项提交。 [原始探针及命令](p16-06-links.log) · [JUnit](p16-06-links-junit.xml)

```bash
p16_manifest_tmp=$(mktemp -d /dev/shm/geopilot-p16-06.XXXXXX)
export PYTHONPATH=src PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 PYTHONDONTWRITEBYTECODE=1
.venv/bin/python -m pytest -q -p no:cacheprovider \
  tests/quality/test_round2_release_manifest.py tests/quality/test_delivery_manifest.py \
  tests/quality/test_ci_matrix.py --basetemp="$p16_manifest_tmp/pytest" \
  --junitxml=docs/round2/deployment/p16-06-tooling-junit.xml
.venv/bin/python scripts/quality/validate_ci_matrix.py
```

新增第二轮清单模块、候选验证入口、复现说明和专项测试。原生成器新增 `--round2`，原校验器新增 `--round2-manifest` 与可选 `--offline-root`。CI 增加新清单专项。撤销工具提交可撤回第二轮候选打包能力。

## 原生基准绑定文件恢复

A 在与正式基准不同的物理盘上归档 Python venv、QGIS CLI 和对应的 LTR Debian 包，解包到空的 tmpfs 目录。源和恢复目录的 7,493 项类型、权限、大小和符号链接逐项一致。恢复目录中的运行时与两份 QGIS CLI 以只读方式挂入已核验的本机镜像，断网启动版本与内容检查，退出 0。运行时 6,738 个条目的指纹及两份 QGIS CLI 摘要与原生环境证据一致。主控独立重算归档 SHA-256，与报告一致。 [恢复及复核报告](p16-06-native-bindings-verification.json) · [原生环境证据](../performance/p13-05-preflight-native-identity.json)

归档为 `artifacts/test-results/p16-06-native-bindings.BdbMRw/p13-native-readonly-bindings.tar.gz`，共 142,078,689 字节，SHA-256 为 `e26482ebf553a85cb572a37fe9adc4bb0c224c4e096e7b2fbdf047dbd22bdef0`。同目录的 `proof/` 保留打包、恢复、容器命令与校验日志。

正式矢量基准结束后，主控用 `docker image save --platform linux/amd64` 导出该固定镜像，归档 `artifacts/test-results/p16-06-native-image.HGV83q/p13-native-image.tar` 共 1,720,453,120 字节，SHA-256 为 `db2d9f1e7e72a3d848a357df5c903278280b6e0d19156c84188e7d770ae41ec3`。同宿主启动初始镜像数为 0 的独立 rootless Docker，导入后镜像 ID 和五个层摘要一致。使用从绑定归档重新解包的文件，断网运行 QGIS/Python 版本及指纹检查，退出 0。宿主 rootless AppArmor 配置查询在合成夹具上报权限错误，隔离复验容器显式指定其 AppArmor 设置后通过。原生复验覆盖镜像、绑定和版本，正式第二轮候选包仍待阶段证据和最终同提交回执。 [离线导入和运行报告](p16-06-native-image-verification.json)

## 已提交证据的链接闭合

主控对提交 `d2de13790c4b3a31b88d495fae5095c8acb1fee6` 的真实 Git 树调用清单生成器的链接校验。依据固定的历史审计索引，发布复现文档、验收总账和三份 P13-05 报告五个入口均通过。验收总账解析到 1,106 个路径，矢量 L 入口包含点相交原始证据归档。随后仅从 `git archive` 导出比较器、点相交归档、预期结果和历史样本。归档摘要、两侧样本及严格配对独立复算通过，结果逐字节一致，退出 0。最终候选包仍需完成阶段验收和同提交回归。 [链接和 Git 导出复验](p16-06-evidence-closure.log) · [点相交复测](../performance/p13-05-vector-l.md)

## 集成预演收集

主控以 `3728ce4dbcf5394b7ebccf390a5691f5d9d95ca9` 为起点，在独立工作树应用两个已冻结的性能候选提交。预演提交 `092fb2d6d09cf45277bea62454e0cae266b50873` 用发布验证入口的后端环境运行 `.venv/bin/python -m pytest -q -p no:cacheprovider --collect-only tests`。Python 3.13.13 和 Psycopg 3.3.4 上收集到 2,145 个唯一节点，JUnit 节点名称也唯一。T01–T16 的 82 个固定节点逐项匹配，退出 0。正式后端、前端和固定 GIS 回归待最终提交执行。 [收集摘要](p16-06-collection-preview.json) · [原始收集日志](p16-06-collection-preview.log.gz)

隔离预演 `96665d486bf32ae2ad711c50043f355e8f64bc74` 纳入局部裁回、分区重投影暂存及状态诊断。后端全量收集 2,163 个唯一节点，按发布校验器转换后的 JUnit 节点键也全部唯一，T01–T16 的 82 个节点均在集合中，退出 0。 [原始收集、JUnit 和预演提交](../performance/p13-05-current-integration-evidence.tar.gz)

组合预演 `a131a5139093047b337302c893d12e709ed04d3b` 的后端收集退出 0，包含 2,178 个唯一节点及唯一 JUnit 键。T01–T16 的 84 条映射引用对应 82 个不同节点，全部逐项匹配。正式同源验证仍待发布提交。 [收集原始记录与准确源码](../performance/p13-05-combined-preview-evidence.tar.gz)
