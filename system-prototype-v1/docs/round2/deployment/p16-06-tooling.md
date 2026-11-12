# P16-06 交付清单与离线校验准备

主控完成打包机制和专项复验。任务整体 DOING，待第二轮最终源提交、同提交验证回执及阶段证据齐备后生成正式候选包。基线为 `51f175fcf03aaf02f8d8580109eb5d50700a74d6` 加本项文件。环境为 Linux x86_64、Python 3.13.13、pytest 8.3.4。

- 候选清单绑定源提交与证据提交，校验两者源文件字节相同。纳入配置、依赖锁、测试、样本、CI、基准配方、选定证据及复现文档。
- 逐用例读取 JUnit，核对计数、实际退出码、同源提交日志和固定 GIS 无跳过。复现文档的本地链接和 Markdown 锚点须有提交对象。
- 归档可按同一输入重复生成相同字节，文件成员取自 Git 提交。离线校验重读解包后的文件指纹和三组 JUnit。历史 V1 清单继续使用原入口。 [生成、校验和离线复现](../../release/ROUND2_REPRODUCTION.md)

本轮 39 项通过、0 失败、0 跳过，退出 0。真实 Git 夹具覆盖两个提交、源码漂移、错误日志、错误 JUnit 计数、缺失链接、归档重复生成、不覆盖现有归档、解包后无 Git 校验以及篡改检测。第二轮实际候选包尚未生成。 [日志](p16-06-tooling-pytest.log) · [JUnit](p16-06-tooling-junit.xml) · [CI 静态核对](p16-06-tooling-ci.json)

补充复验基于 `ddf4e6074b265626690aef3986d013776acb5bec` 和本项变更。生成及离线核验逐一检查固定 GIS JUnit 中的 T01–T16 映射节点。缺失或重复节点时拒绝归档。按同一测试文件复跑 41 项通过、0 失败、0 跳过，退出 0。历史 JUnit 中的 476 个节点包含映射要求的 82 个唯一节点，逐项匹配。正式候选仍待同源提交复验。 [日志](p16-06-coverage-pytest.log) · [JUnit](p16-06-coverage-junit.xml)

```bash
p16_manifest_tmp=$(mktemp -d /dev/shm/geopilot-p16-06.XXXXXX)
export PYTHONPATH=src PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 PYTHONDONTWRITEBYTECODE=1
.venv/bin/python -m pytest -q -p no:cacheprovider \
  tests/quality/test_round2_release_manifest.py tests/quality/test_delivery_manifest.py \
  tests/quality/test_ci_matrix.py --basetemp="$p16_manifest_tmp/pytest" \
  --junitxml=docs/round2/deployment/p16-06-tooling-junit.xml
.venv/bin/python scripts/quality/validate_ci_matrix.py
```

新增第二轮清单模块、复现说明和专项测试。原生成器新增 `--round2`，原校验器新增 `--round2-manifest` 与可选 `--offline-root`。CI 增加新清单专项。撤销工具提交可撤回第二轮候选打包能力。
