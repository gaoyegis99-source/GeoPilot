# P13-06 中间成果复用的证据准备

P13-06 等待 P13-05 验收。F 和主控核对现有确认方案、步骤请求、运行检查和指纹。主控在 `b56fe904b76674dc505333f3c390e4f3626a2888`、Linux 5.15、Python 3.13.13 上复跑 14 项现有测试，全部通过。两个实际函数探针均退出 0，与 F 的结果逐字节一致。 [执行命令及原始结果](p13-06-preflight.log) · [JUnit](p13-06-preflight-junit.xml)

| 复验 | 实际观察 | 对缓存身份及验证的约束 |
|---|---|---|
| 内容覆盖、GPKG 层、外部文件 | 同路径、大小和 mtime 的内容覆盖改变内容指纹。同一 GPKG 的不同层可有相同文件指纹，层身份不同。GPKG WAL、TIFF 掩膜和 PAM 会改变文件组指纹。 | 键使用输入角色、实际图层和内容文件组指纹。 |
| QGIS 参数构造 | `StepRequest` 和工具引用完全相同。仅更改一份可通过验证的枚举绑定，生成的实际 `PREDICATE` 从 0 变为 6。 | 方法和工具版本、归一化参数之外，需识别实际工具契约、执行绑定、计算实现及相关配置。 |
| 暂存路径与来源 | 两份暂存 GPKG 的内容指纹相同，遥感工具生成的来源记录含各自的运行内相对路径。 | 复用需产生符合当前运行的来源记录，并复验输出图层、类型及完整指纹。 |
| 前序结果与登记 | 编译器拒绝旧 run 的结果、失败结果、错层及类型不符的输出。登记在准备和提交之间检测到文件变化时拒绝。 | 缓存需要明确来源运行及步骤。命中后仍走本次成果复验，缓存损坏或缺失可回到正常计算。 |

现有步骤请求保留工具引用、参数和运行内路径。常规运行尚缺实际绑定和运行时构建身份的持久化证据。发布工具已有安装、源码和配置摘要可供复用。现有进程检查按本次请求核对结果、实际 argv 和环境，复制旧步骤日志不足以证明本次执行。复用来源的校验须随正式实现验证。 [指纹](../../../src/geopilot/data/fingerprint.py) · [QGIS 绑定](../../../src/geopilot/execution/qgis_runner.py) · [步骤编译](../../../src/geopilot/plans/compiler.py) · [进程检查](../../../src/geopilot/checks/process_checks.py) · [发布证据](../../../scripts/methods/release.py)

两个探针及校验清单保存在 `artifacts/test-results/p13-06-preflight.kNXIL4/SHA256SUMS`。本次为只读验证，生产 API、配置和存储契约无变更。

## 正式样本中的图层身份与成果指纹

主控与 A 从已归档的 P13-05 修正版证据核对 M 档四组和 L 档两组的 84 个通过样本，其中 60 个为正式计量。每个负载的独立验算报告和检查规则结果在 14 次运行中一致。各 GeoPackage 成果角色的 14 个登记指纹均不同。按面积分配的分配明细 CSV、分区统计 JSON 和道路统计 CSV 则各自只有一个指纹。已验证三份证据归档及可用的文件清单，复算退出 0。 [复算脚本](p13-06-output-fingerprints-audit.py) · [实际样本统计](p13-06-output-fingerprints.json)

## 栅格成果文件组复算

主控读取冻结修正版 `a2d2e68` 的 DEM S、DEM M 和遥感 S 三个已封存分片。每组冷暖各 7 轮，共 42 个不同运行，均取得 `SUCCEEDED/PASSED`。DEM M 中三轮保留正式基准的状态观测失败标签，同时具有已核对的成果和检查报告。每个负载的确认参数、实际执行参数、独立正确性报告和规则结果一致。

从登记清单定位并重算 224 个实际成果文件组，全部与登记指纹相同。同一负载的每个成果角色在 14 次运行中各有一个指纹。DEM S 与 DEM M 每组包含三项成果，遥感 S 包含十项，覆盖 GeoTIFF 和 JSON。原始报告、配置回执、登记清单及文件组随归档提供，离线复算退出 0。 [原始输入、复算命令及结果](p13-06-raster-output-fingerprints-evidence.tar.gz)

在隔离的阈值 GeoTIFF 副本中加入 `.tif.aux.xml`，主 TIFF 的 SHA-256 一致，文件组指纹改变。移除辅助文件后恢复原指纹。修改另一份解包副本中的 TIFF 内容后，审计拒绝该成果，退出 1。后续缓存命中需重算已登记文件组并复验成果。P13-06 等待 P13-05 验收。

```bash
p13_raster_cache=$(mktemp -d)
tar -xzf docs/round2/performance/p13-06-raster-output-fingerprints-evidence.tar.gz -C "$p13_raster_cache"
(cd "$p13_raster_cache" && sha256sum -c SHA256SUMS)
.venv/bin/python -B "$p13_raster_cache/proof/verify.py" "$p13_raster_cache"
```

L 点相交与线相交使用同一 `relations.gpkg` 文件和输入清单，同一方法、工具版本及记录的步骤参数。28 次实际输入检查日志分别记录打开 `points` 和 `lines`。两组独立验算分别确认保留 65,536 个点和 131,072 条线，后者总长 4,030,464 米。 [点相交原始样本](p13-05-vector-l-point-evidence.tar.gz) · [线相交原始样本](p13-05-vector-l-line-evidence.tar.gz) · [M 档原始样本](p13-05-vector-m-revised-evidence.tar.gz)

缓存键需要包含每个输入角色的实际图层及文件组指纹。命中时可复用现有 `copy_file_group`，按缓存条目登记的物理指纹校验副本，再按本次运行重新打开成果、核验来源和独立正确性。运行日志证明图层与物理指纹存在差异，GeoPackage 中导致物理差异的字段未核对。P13-06 仍等待 P13-05 验收。 [文件组复制](../../../src/geopilot/data/fingerprint.py#L132) · [成果重新打开与登记](../../../src/geopilot/artifacts/service.py#L137)

```bash
p13_fingerprints=$(mktemp -d)
for p13_archive in p13-05-vector-m-revised p13-05-vector-l-point p13-05-vector-l-line; do
  mkdir "$p13_fingerprints/$p13_archive"
  tar -xzf "docs/round2/performance/$p13_archive-evidence.tar.gz" -C "$p13_fingerprints/$p13_archive"
done
.venv/bin/python -B docs/round2/performance/p13-06-output-fingerprints-audit.py "$p13_fingerprints" \
  | cmp - docs/round2/performance/p13-06-output-fingerprints.json
```
