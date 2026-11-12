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

## 集成提交的前端与后端收集预验

主控从 `cf53e827a93ff4f56cca8541827b26ff858ed736` 的 Git 树导出前端，在 Linux 5.15、Node v22.23.1、npm 10.9.8 上用锁定依赖离线安装。生产构建、16 个 Vitest 文件中的 73 个用例均通过，三个命令退出 0。构建保留 Vite 的体积提示，主 JS 压缩前约 1.39 MB。 [安装、构建、JUnit 和实际命令](p16-06-frontend-source-preview-evidence.tar.gz)

同一提交的独立净工作树通过发布验证入口收集后端。2,171 个唯一节点包含 T01–T16 的全部 82 个固定节点，退出 0。与此前六项性能预演相比，新增 2 个状态查询序号测试，少 9 个尚未合入的投影布局候选测试。预演入口已开始同一提交的后端执行，正式发布证据仍等待最终源码和选定的阶段验收。 [原始收集、节点差异及预演启动命令](p16-06-frontend-source-preview-evidence.tar.gz)

## 全量预演发现的版本断言

首次后端预演在三个方法版本兼容节点观测到失败。精确源码的独立复跑确认均为固定版本数量断言，CLI 实际保留 56 个版本。修正后兼容与路网专项 24 项通过。首次全量记录继续保留，修正后的完整后端与固定 GIS 仍需同提交预演。 [原因、修改和实测](../methods/p14-06.md#基线版本数量的回归修正)

首次完整预演在 `cf53e827a93ff4f56cca8541827b26ff858ed736` 结束。前端 73 项通过，后端 2,168 通过、3 失败、0 跳过，2,171 个 JUnit 节点与完整收集逐项一致。后端耗时 1,038.51 秒，验证入口退出 1，独立固定 GIS 阶段为 NOT_RUN。已从修正提交 `d1a9d4a68dc73ca355d13d013925d5e0fe518972` 启动新净工作树复验，沿用离线依赖、真实 QGIS 和 PostGIS。 [首次完整日志、JUnit、节点复算和重试回执](p16-06-initial-full-source-preview-evidence.tar.gz)

## 修正提交的完整预演

在干净分离检出 `d1a9d4a68dc73ca355d13d013925d5e0fe518972`，正式验证入口退出 0。前端离线依赖安装、TypeScript 检查和 Vite 构建通过。后端 2,171 项、前端 73 项、独立固定 GIS 82 项通过，各套件均为 0 失败、0 跳过。后端耗时 1,042.71 秒，固定 GIS 97.71 秒。 [原始日志、命令、JUnit 和独立复算](p16-06-corrected-full-source-preview-evidence.tar.gz)

B 将后端 JUnit 与 2,171 个收集节点逐一核对，将固定 GIS JUnit 与 T01–T16 的 82 个唯一映射节点核对。前端 73 条记录对应 16 个测试文件，其中两组参数化测试复用了显示标题，独立审核保留参数化记录计数。主控从便携包重放退出 0。该源码早于 `f16f72d` 跨批复用合入及 P13-06 缓存实现。正式发布仍需在最终源码复跑。

## 同提交技术候选包验收

主控与 B 在净分离检出 `44b17fcd5dc09ec9d3d713f2dddf8fdbcf3600b0` 验证，原始证据提交为 `9ea45ec991547adf0b70e16710efb8f789450183`。P13 与 P14 已选范围均已通过。验证使用 Linux 5.15、Python 3.13.13、pytest 8.3.4、QGIS 3.44.15、Psycopg 3.3.4、Node v22.23.1 和 npm 10.9.8，固定离线 npm 缓存，真实 QGIS 与 PostGIS。启动脚本、CPU 限制包装器和各套实际命令随证据封存。 验证回执收录在本节原始证据中。

| 套件 | 通过 | 失败／错误／跳过 | 退出码 |
| --- | ---: | ---: | ---: |
| 前端离线安装、TypeScript、Vite 构建及 Vitest | 73 项，16 文件 | 0／0／0 | 0 |
| 后端完整收集与回归 | 2,228 项，1,088.86 秒 | 0／0／0 | 0 |
| T01–T16 独立固定 GIS | 82 项，97.17 秒 | 0／0／0 | 0 |

独立复核逐项匹配 2,228 个唯一后端节点和 82 个唯一固定 GIS 节点。前端的两组参数化测试保留相同展示名称，按 Vitest 和 JUnit 的实际执行条数核对。Vite 保留构建体积提示，后端保留依赖弃用提示和地理数据写入警告。 [独立审计、原始命令和回执](p16-06-candidate-verification-evidence.tar.gz)

主控运行 `delivery_manifest.py --round2 --source-ref … --evidence-ref … --archive … --write` 生成清单与确定性归档。按提交核验与空目录解包后无 Git 核验均退出 0，JSON 字节一致。包内有 944 个源码范围文件、733 个证据文件，附版本、锁定依赖、测试、样本和离线复现文档。发布归档的 SHA-256 为 `7971fce3e321224a8a7dc6f9dc7c365ffd4cff90de79e18fec5e417b971cfbc3`。源码和证据文件按各自精确提交保存。可按固定引用重建；撤销本次交付文档和清单提交可从开发分支移除候选引用。 [复现说明](../../release/ROUND2_REPRODUCTION.md)

P16-06 主控 ACCEPTED。该归档为已验证技术候选。真实用户观察与后续体验改进仍是 P16-07 发布决策的前置，若源码变化需对届时版本重跑并更新包。

### 下一版候选的证据链接复查

主控在 Python 3.13.13 读取净 Git 树，按复现说明、验收日志和并发决策三个入口调用发布生成器的 `_reproduction_links`。首次发现历史报告引用了可重新生成的清单路径，下一版打包会将清单自身列为证据。修复后旧回执从固定验证证据包查看，新一轮发布仍可引用旧证据和本轮受试演练。首次禁入清单检查退出 1。修复后从原型目录用以下命令比较两份 Git 树，退出 0。

```bash
PYTHONPATH=scripts/quality .venv/bin/python -B - <<'PYTHON'
from pathlib import Path
from build_source_inventory import git
from round2_release_manifest import MANIFEST, _historical_links, _read_file, _reproduction_links, _scope
from verify_delivery_manifest import committed_files

root = Path.cwd().parent
prefix = "system-prototype-v1/"
roots = {prefix + path for path in (
    "docs/release/ROUND2_REPRODUCTION.md", "plans-round2/06_PROGRESS/ACCEPTANCE_LOG.md",
    "docs/round2/decisions/p13-08.md",
)}
for rev, expected in (("bbfc1d8", True), ("13b9b6c", False)):
    commit = git(root, "rev-parse", rev).decode().strip()
    tree = committed_files(root, commit)
    audit = _read_file(root, tree, prefix + "docs/round2/audit/evidence-link-audit.csv")
    links = _reproduction_links(roots, set(tree), _scope(tree, prefix),
                                lambda path: _read_file(root, tree, path), _historical_links(audit))
    circular = prefix + MANIFEST.as_posix() in links
    print(f"{commit}: {len(links)} evidence links, generated manifest in evidence = {circular}")
    assert circular is expected
    assert prefix + "docs/round2/user-research/p16-03-rehearsal-evidence.tar.gz" in links
    assert prefix + "docs/round2/deployment/p16-06-candidate-verification-evidence.tar.gz" in links
PYTHON
```

```text
bbfc1d8f6bca9259f004f6a95ca39dbe8a218dac: 1198 evidence links, generated manifest in evidence = True
13b9b6c91fdf139798df3974b3eac94ed96633a3: 1196 evidence links, generated manifest in evidence = False
```

现有候选另用 `.venv/bin/python -B scripts/quality/verify_delivery_manifest.py --round2-manifest artifacts/reports/final/round2-delivery-manifest.json` 复核，退出 0，与封存核验 JSON 逐字节一致。候选归档与清单的 `sha256sum -c SHA256SUMS` 均退出 0。回滚可撤销历史链接修复和此记录。

## 工作台修复后的技术候选

主控、F 和 E 复核更新候选。源提交 `6ecdbca38671b28793244d90984d116411bb4e52`，证据提交 `3409fe7b3fecbe39d04b5cf353ca0aab0cd3faa7`。候选包含 CRS 定位、取消重跑、执行与检查状态区分、运行历史及成果归属。验证在净分离检出上使用 Linux 5.15、Python 3.13.13、pytest 8.3.4、QGIS 3.44.15、Psycopg 3.3.4、Node v22.23.1 和 npm 10.9.8，使用离线 npm 缓存及真实 QGIS 和 PostGIS。 [修复复验](../user-research/p16-ux-follow-up.md)

| 套件 | 通过 | 失败／错误／跳过 | 退出码 |
| --- | ---: | ---: | ---: |
| 前端安装、构建与 Vitest | 75 项，16 文件 | 0／0／0 | 0 |
| 后端全量 | 2,255 项，1,104.12 秒 | 0／0／0 | 0 |
| T01–T16 独立固定 GIS | 82 项，97.17 秒 | 0／0／0 | 0 |

独立审计逐项核对后端唯一节点和固定 GIS 映射。技术候选包含 946 个源码范围文件和 742 个证据文件。按 Git 及空目录解包的验证均退出 0，输出字节一致。交付归档 SHA-256 为 `981473df690bc043ff0e8aa7e2ebda44d4f5e613b07729cd84c1157fcf1bf103`。打包命令、原始测试报告和独立审计见证据归档，SHA-256 为 `5de81c19efc216b4d20f9d9c41b7002acb92b19e6daa866f42c833bf2a9da10a`。 [候选原始证据](p16-06-ux-candidate-evidence.tar.gz)

P16-06 更新候选验收为 ACCEPTED。按原 Git 源提交与证据提交可复建上一候选 `44b17fc`。新版附主持人补充观察指南，P16-03 真实用户观察以及 P16-04、P16-05 的真实体验验收仍待完成。P16-07 实际前置核验退出 1，候选匹配，列出的未齐依赖仍为这三项。 [主持人补充指南](../user-research/moderator-ux-follow-up.md)

## 检查取消恢复候选

主控、E、F 复核候选源码 `6961ee3694694be24f58697fe666d8b0b67fee01`，证据提交 `b9fb0a19ff414644a8297c03e79193a089962286`。净分离检出在 Linux 5.15、Python 3.13.13、pytest 8.3.4、QGIS 3.44.15、Psycopg 3.3.4、Node 22.23.1 和 npm 10.9.8 下执行，使用离线 npm 缓存及真实 QGIS 和 PostGIS。独立 Chromium 对照验证了检查取消后的中文提示和唯一重查入口。 [交互复验](../user-research/p16-check-recovery.md)

| 套件 | 通过 | 失败／错误／跳过 | 退出码 |
| --- | ---: | ---: | ---: |
| 前端安装、构建与 Vitest | 76 项，17 文件 | 0／0／0 | 0 |
| 后端全量 | 2,255 项，1,096.39 秒 | 0／0／0 | 0 |
| T01–T16 独立固定 GIS | 82 项，97.87 秒 | 0／0／0 | 0 |

F 独立核对三套 JUnit 节点，后端与收集清单一致，固定 GIS 与 T01–T16 命令目标一致。包内 947 个源码范围文件、747 个证据文件，Git 和空目录解包的校验输出字节一致。交付归档 SHA-256 为 `29c5be0fefda1bc2742d020a9d1bcfdea70623bf4db3a163cb286bb5668f827e`。验证命令、回执、日志、JUnit 和两份独立审计见证据归档，SHA-256 为 `ca47408a5fbdaf982fc2f27b4aa7263f3bd8d04b9bc33d91cadd3ca638779494`，解包 67 个文件校验通过。 [候选原始证据](p16-06-check-recovery-candidate-evidence.tar.gz)

P16-06 更新候选验收为 ACCEPTED。工作台检查取消恢复仅影响前端展示与入口。可按既有源码及证据提交重建候选 `6ecdbca`。P16-07 前置核验退出 1，当前候选匹配，未齐依赖为 P16-03 至 P16-05。 [发布前置核验](../release/p16-07-preflight.md)
