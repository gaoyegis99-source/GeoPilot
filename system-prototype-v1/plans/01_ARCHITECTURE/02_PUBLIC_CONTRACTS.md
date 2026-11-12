# 公共接口契约冻结清单

以下对象属于跨 Agent 共享契约，先由主控定义/确认，再由专业 Agent 使用：
- DataAssetRef：asset_id + asset_version。
- DataAsset：source_type、metadata、fingerprint、version、availability。
- MethodDefinition：id/version、inputs、preconditions、critical_options、locked_order、steps、expected_artifacts、check_rules。
- ToolContract：id/version、runner、implementation、inputs、outputs、parameters、measurement_policy、preconditions。
- AnalysisPlan：task、inputs、method_ref、critical_options、steps、expected_artifacts、check_rules、issues、status、version。
- StepRequest：run_id、step_key、tool_ref、inputs、parameters、output_targets、workdir_ref、log/cancel refs。
- StepResult：status、exit_code、outputs、log_path、warnings、error。
- Run：plan_id/version、execution_status、check_status、current_step、cancel_requested、error。
- Artifact：run_id、role、uri、format、fingerprint、metadata、source refs。
- CheckReport：report_id、run_id、rule_results、summary_status、rule_version。

## 状态契约
Execution：NOT_STARTED / RUNNING / SUCCEEDED / FAILED / CANCELLED。
Check：NOT_CHECKED / CHECKING / PASSED / WARNING / FAILED。
两套状态独立，不得相互覆盖。

P2-08 至 P2-10 共用[步骤执行交接](07_STEP_PROTOCOL.md)。

## 错误对象
至少包含 code、phase、step_key、field、message、suggestion、log_path。错误码沿用设计附录 B；新增错误码需登记。

## API 基线
沿用 V2.0 第 8.6 节接口意图：assets、tasks/context、plans/confirm、plans/runs、runs/status/cancel/artifacts/checks、artifact preview/file/value。实际路由前缀和版本须在 P0 审计后确认。

## 变更流程
任何字段增删、枚举变更、序列化变化、数据库关系变化均须提交变更提案并同步上下游测试。

## P1-01 SQLite 基线

依据 V2.0 §8.1、§12.3.2。迁移入口位于 `geopilot.persistence.schema`。`initialize_database(path) -> int` 原子升级并返回 SQLite `user_version`；`open_database(path)` 为连接上下文管理器，开启外键、使用 `sqlite3.Row`，离开上下文时关闭，调用方显式管理业务事务。迁移先获取写事务再读取版本，失败回滚，拒绝高于当前支持版本的数据库。

v1 基表为 `data_asset`、`task`、`analysis_plan`、`run`。`data_asset` 以 `(id, version)` 为主键，记录 `source_key`、`source_type`、`source_json`、`fingerprint`、`metadata_json`、`availability`、`created_at`；`(source_key, version)` 唯一。`source_key` 在登记前由规范化本机来源与明确图层生成，同一来源用同一 `id`。`task` 记录 `id`、`goal_json`、`context_json`、`created_at`，上下文未生成时允许为空。`analysis_plan` 以 `(id, version)` 为主键，引用 `task_id`，保存 `status`（`draft` 或 `confirmed`）、`plan_json`、`created_at`。`run` 以 `id` 为主键，引用 `(plan_id, plan_version)`，分别保存 `execution_status`、`check_status`、`workdir`、`error_json`（可空）、`created_at`。版本为正整数；空间文件由来源和工作目录引用。P3/P4 接入各自服务与后续迁移。

## P1-02 资产对象

`geopilot.domain.data_asset` 定义 `DataAssetRef(asset_id, asset_version)`、`DataAsset(id, version, source_type, source, metadata, fingerprint, availability, created_at)`，`DataAsset.ref` 返回版本引用。版本要求严格正整数。`SourceType` 值为 `local_file` 和 `postgis_snapshot`；P1 只登记本机文件。`AssetAvailability` 值为 `AVAILABLE` 和 `UNAVAILABLE`。`FileSource(path, layer=None, modified_ns=None)` 保存本机绝对路径、明确图层和可取得的修改时间（纳秒）。序列化的 `source` 仅供本机持久化和服务使用；模型侧另建允许字段对象。

`AssetMetadata` 按 `kind` 区分 `vector`、`raster`、`table`，均有 `format`（驱动名）、`crs`（已知时用 CRS 标识或 WKT，未知时 `null`）、`bounds`（`[min_x, min_y, max_x, max_y]` 或 `null`）、`issues`（原因代码列表）。`VectorMetadata` 保存 `geometry_type`、`feature_count` 和 `fields`，`TableMetadata` 保存 `row_count` 和 `fields`。`FieldMetadata` 为 `name`、`dtype`、`missing_count`。`RasterMetadata` 保存 `width`、`height`、`band_count`、`transform`（Rasterio 的 `[a,b,c,d,e,f]`）、`resolution`（坐标系单位）、逐波段 `nodata`、`band_dtypes`、`band_names`、`band_units`。缺失 NoData 为 `null`，非有限 NoData 值用 `"nan"`、`"inf"`、`"-inf"` 表达，JSON 中与缺失区分。空间元数据缺少 CRS 时可保留记录，登记结果须有 `CRS_UNKNOWN` 原因；禁止猜测 CRS。

## P1-03 至 P1-06 读取与指纹

读取器接收本机文件路径，返回资产元数据，不保存读取句柄。`geopilot.data.vector_reader.read_vector_metadata(path)` 支持本机 GeoJSON 和 Shapefile。共用的 `read_layer_metadata(path, layer=None)` 提取 OGR 矢量/表格元数据，供 GeoPackage 读取器复用。`geopilot.data.geopackage_reader.list_geopackage_layers(path)` 返回名称和几何类型（无几何为 `None`）的序列；`read_geopackage_metadata(path, layer=None)` 返回 `(selected_layer, metadata)`，单图层可推定，多图层需要明确选择。`geopilot.data.raster_reader.read_raster_metadata(path)` 支持 GeoTIFF/COG，只读取元数据。

三个读取器共用 `geopilot.data.errors.SourceReadError(message, *, field="source.path")`，`code="SOURCE_UNREADABLE"`、`phase="data"`，字段可用 `source.layer` 定位。缺少 CRS 的可读数据通过 `metadata.crs=null` 和 `issues` 表达，不作为读取异常。`geopilot.data.fingerprint.fingerprint_file(path, *, strategy="content")` 返回 `FileFingerprint(digest, size_bytes, modified_ns)`。`content` 使用流式 SHA-256，保留真实内容身份；`metadata` 为大小和时间等元数据摘要，用于廉价变化提示，不作为版本内容等价证据。复合格式的相关旁车文件进入同一摘要，登记和重开默认用 `content`。

## P1-07 至 P1-09 登记与重开

`geopilot.data.assets.register_asset(database_path, path, *, layer=None) -> DataAsset` 使用已初始化的数据库。选择本机 GeoJSON、Shapefile、GeoPackage 或 GeoTIFF/COG 适配器；`layer` 仅用于 GeoPackage。保存解析后的绝对路径与所选图层。由来源类型、路径和图层生成稳定 `source_key`，在同一 SQLite 写事务中查版本及写入。相同内容返回原资产和版本；初次登记创建 v1。P1-08 为已变内容新增版本并保留原记录。同一路径的不同图层是不同资产。

读取元数据及计算内容指纹时比较前后文件状态。`geopilot.data.fingerprint.capture_file_state(path)` 返回可比较的进程内状态，涵盖文件组成员及 inode、大小和时间等信息。状态不持久化，不改变两种指纹策略。读取失败或不一致时不创建记录，缺少 CRS 的可读空间源保留 `CRS_UNKNOWN` 与可用的文件引用。`geopilot.data.assets.InputChangedError` 使用 `INPUT_CHANGED`。P1-09 的 `geopilot.data.resolver.resolve_asset(database_path, ref) -> DataAsset` 用确切版本重开源文件并验证摘要与指纹，结束读取句柄后返回记录；不存在的引用使用 `LookupError`，缺失文件或图层为结构化来源错误。历史版本保留元数据和指纹，原始文件改写时旧引用需要匹配的内容才能再次运行。

## P1-10 运行目录

`geopilot.persistence.workspace.create_run_workspace(root, task_id, run_id) -> RunWorkspace` 根据 V2.0 §8.2 创建独立目录，调用方分配 ID。同一 ID 组合重复创建时报错。`RunWorkspace.path` 是运行根；`request_path`、`cancel_path` 提供相应文件位置；`create_step(step_key)` 返回新建步骤目录，其内部文件名为 `request.json`、`result.json`、`process.log`。仅准备文件位置，写入由运行服务负责。ID 和步骤名不得包含路径分隔符、路径跳转或空白；路径保持在工作区内，禁止穿越工作区内部符号链接。目录创建失败需保留可定位路径和原因。根目录布局沿用 V2.0 §8.2，不改原始数据。

## P2-01 工具契约

`geopilot.domain.tool_contract` 按 V2.0 §7.5、§8.4 定义 `ToolRef(id, version)`、`ToolContract`、`ParameterSpec` 与 `MeasurementPolicy`。`ToolContract` 必填 `id`、`version`、`runner`（`qgis` / `python`）、`implementation`（本地算法或登记工具标识）、`inputs`、`outputs`、`parameters`、`measurement_policy`、`preconditions`、`check_rules`。`ref` 为确定版本的 `ToolRef`。版本为严格正整数。输入输出为端口名到数据类型名的映射；前提和规则为有序名称。允许空映射或空规则列表的工具使用显式空集合。

`ParameterSpec` 支持 `number`、`integer`、`boolean`、`enum`、`string`、`json`。`unit` 仅可用于数值型；可选上下界为 `minimum`、`maximum`、`minimum_exclusive`、`maximum_exclusive`，同侧不能混用。`values` 仅用于 `enum`，须为非空且唯一的字符串值。`required` 默认 `true`；可选 `default` 的类型、范围和枚举值必须合法，`null` 表示无默认值。P2-03 负责方案参数运行时规范化及显式单位转换，运行器参数名和枚举值的映射由本机工具注册配置负责。

`MeasurementPolicy.distance` 取 `local_projected`、`geodesic` 或 `not_applicable`，`area` 取 `equal_area` 或 `not_applicable`。两个字段显式提供。`forbid_web_mercator` 和 `forbid_geographic_planar` 默认 `true`；平面测量契约不能关闭。模型只检查结构，不把策略声明当成真实 CRS、单位或测地线实现证据。

`ParameterSpec.validate_value(value)` 验证已采用契约单位的值，失败抛 `ValueError`。默认值使用同一校验。

## P2-02 至 P2-04 注册、参数与 CRS

- `geopilot.tools.registry.ToolRegistry(*, qgis_process=None, python_implementations=())` 登记 `ToolContract` 和可选 `required_modules`。`get(tool_id, version=None)` 返回 `RegisteredTool(contract, available, reason)`，省略版本选已登记的最高版本，找不到抛 `KeyError`；重复版本抛 `ValueError`。`list(*, runner=None, available_only=False)` 按 `(id, version)` 稳定排序。QGIS 实测 CLI JSON 算法清单，Python 可用性取明确登记的本机实现 ID 与依赖，缺项说明原因。注册表不执行任意工具。查询返回的内容不能修改已登记的契约。
- `geopilot.tools.parameter_normalizer.normalize_parameters(contract, values)` 返回普通 JSON 参数表，逐键复用 `ParameterSpec.validate_value`，展开声明的默认值。普通数值已采用契约单位；显式换算输入为 `{"value": 数值, "unit": 单位}`，只转换已登记的同量纲单位。保留严格类型和枚举，不转换数值字符串或布尔值。缺少必填项、未知参数或无效值抛 `ParameterNormalizationError`，`code=INVALID_PARAMETER`、`phase=preflight`、`field=parameters.<键>`。
- `geopilot.gis.validation.crs_validator.inspect_crs(metadata, *, field="inputs.source.crs", require_projected_metres=False)` 返回 `CRSInfo(crs, horizontal_unit, metres_per_unit, is_web_mercator)`，其中 `crs` 为 `pyproj.CRS`。元数据源自真实读取器，禁止依据范围猜 CRS。要求投影米制时拒绝经纬度、Web Mercator（含等价定义）及非米制水平单位。`validate_crs_operation(source_crs, target_crs, *, operation)` 区分 `assign`（仅对未知源显式指定）与 `reproject`（源和目标均已知），只验证操作条件。结构化 `CRSValidationError` 包含 code（`CRS_UNKNOWN` 或 `MEASUREMENT_POLICY_REJECTED`）、phase（`preflight`）、field、message 和 observed。范围适用性由 P2-05 至 P2-07 验证。

## P2-05 至 P2-07 测量与投影

`geopilot.gis.measurement.coverage.geographic_bounds(crs, bounds, *, field)` 经 PROJ 将实际输入坐标范围转换成 WGS84 范围。`verify_projection_coverage(crs, bounds, *, field)` 另核对投影声明的适用区，返回 `(west, south, east, north)`。覆盖依据不足、转换失败或跨日期变更线的单矩形范围在 P2 阻断。`MeasurementPolicyError` 携带 `code=MEASUREMENT_POLICY_REJECTED`、`phase=preflight`、`field`、`message`、`observed` 和可选 `suggestion`。`MeasurementDecision(method, crs, unit)` 保存通过的方式、实际 CRS 和规范单位。

`geopilot.gis.measurement.distance.validate_distance(metadata, *, method="local_projected", field="inputs.source.crs")` 复用真实 CRS 检查和适用区，局部距离使用米制投影。以 0.001 最大相对线性变形作为 P2 的保守适用条件，在范围内检查 PROJ 的 Tissot 主比例因子并记录检查口径。`geodesic` 在真实工具接入前明确返回不可用，T04 在 P6 验收。`geopilot.gis.measurement.area.validate_area(metadata, *, field="inputs.source.crs")` 要求真实的米制等积方法与适用区，记录 `equal_area`、CRS、`square_meter`。只按真实投影方法识别等积，不能从 CRS 显示名推定。正向固定用例要检查实际进程、参数及几何或数值。

`geopilot.gis.measurement.projection.select_measurement_crs(metadata, *, method, target_crs=None)` 返回 `MeasurementDecision`，方法为 `local_projected` 或 `equal_area`。自动候选分别为完整覆盖的唯一 WGS84 UTM 区与 EPSG:6933，随后由相同测量策略核对候选和转换范围。显式目标同样验证。 `reproject_vector(source, destination, *, method, target_crs=None, source_layer=None, output_layer=None)` 用已知源 CRS 转换真实几何，写入新 GeoPackage，返回 `ReprojectionResult(path, layer, decision)`，默认层为 `projected`。Web Mercator 可作为已知转换源，测量目标仍遵循投影策略。

## P3-01 方法定义

`geopilot.methods.models` 按 V2.0 §7.4、§8.5 和附录 A 定义 `MethodRef(id, version)` 与 `MethodDefinition(id, version, locked_order, inputs, critical_options, preconditions, steps, expected_artifacts, check_rules)`。版本为严格正整数，`locked_order` 为布尔值，输入是角色到类型名的映射。步骤、成果、前提和规则保持声明顺序，`preconditions` 可省略为空。

数值等类型化选项复用 `ParameterSpec`，字符串数组声明枚举。引用选项可用 `{type: reference, required_for: 条件}`，附录中的 `user_defined`、`user_selected` 保留为示例占位。完整配置通过后续静态和编译校验后才能运行。

每步 `MethodStep` 至少有 `key`，可附精确 `tool: {id, version}`、端口输入、参数绑定和命名输出类型。输入形式为 `{asset: 角色}` 或 `{step: 步骤, output: 名称}`，参数形式为 `{option: 选项}` 或 `{literal: JSON 值}`。成果声明保留有序名称，也允许 `{role, step, output, type}` 的明确绑定。方法定义保存模板，方案负责具体资产版本与参数值，静态校验负责实际工具、引用、顺序和可用性。

## P3-02 方法目录

`geopilot.methods.loader.load_method_catalog(directory) -> MethodCatalog` 读取目录内 `.yaml` 和 `.yml` 配置，稳定排序。目录提供 `get(method_id, version=None)` 与 `list()`，返回隔离的 `MethodDefinition`；省略版本时取最新。单文件异常通过 `issues` 保存 `MethodLoadIssue(path, field, message)`，重复的同 ID 同版本配置记录各自文件位置。其他有效方法和版本仍可查询。可执行性由 P3-03 校验。

## P3-06 方案模型与 P3-07 上下文前置

`geopilot.domain.task.TaskGoal(summary)` 表示 V2.0 §8.3 的用户目标。 `geopilot.plans.models.AnalysisPlan` 使用 `id`、`task_id`、`version`、`status`、`goal: TaskGoal`、`inputs: {role: DataAssetRef}`、`critical_options`、`method: MethodRef`、有序 `steps`、`expected_artifacts`、`check_rules`、`issues`。 `PlanStatus` 取 `draft` 和 `confirmed`，已确认方案需有步骤及全部工具版本，问题列表为空。

`PlanStep(key, tool, inputs, parameters, outputs)` 复用方法的资产角色和前序输出引用，参数保存解析后的有限 JSON 值，成果引用使用 `PlanOutput(type, target, layer=None)` 的运行目录相对目标。草稿步骤允许 `tool=null`。预期成果复用 `ExpectedArtifactBinding(role, step, output, type)`，问题使用 `PlanIssue(code, field, message)`。跨步骤解析和真实前提在 P3-04 至 P3-09 验证。

P3-07 的任务上下文前置复用 P1 的真实资产版本重开服务。 `geopilot.application.task_context.build_task_context(database_path, *, task_id, goal, method, selected, registry=None)` 根据用户显式指定的角色与 `DataAssetRef` 生成 `TaskContext(task_id, goal, method, roles)`，按角色记录实际版本、`READY`、`CONDITIONAL`、`BLOCKED` 或 `UNKNOWN` 及原因。传入注册表时复用直接绑定到资产角色的测量策略。前序成果的实际测量条件由对应步骤执行前检查。上下文生成及其测试随 P3-07 前置验收。

## P3-03 与 P3-04 配置和依赖校验

`geopilot.methods.validator.validate_method(method, *, registry, registered_rules, source=None)` 返回有序 `MethodIssue(code, field, message, path)`。检查配置步骤、工具精确版本及可用性、端口与参数绑定、预期成果角色和规则注册。可传入配置文件路径，字段使用点分位置。结构示例可以加载，缺少可执行绑定时产生校验问题。

`geopilot.plans.dependency_validator.validate_dependencies(input_roles, steps)` 返回有序 `DependencyIssue(code, field, message)`，使用步骤的 `key`、`inputs` 和 `outputs`，供方法模板和展开方案复用。检查资产角色、前序步骤和命名输出的存在性，拒绝重复步骤键、循环及未来引用。验证过程保留配置顺序。

`geopilot.plans.step_compiler.validate_step_order(method, steps)` 返回 `DependencyIssue` 序列。对 `locked_order=true` 的方法逐项比较步骤键并定位首处不同，增加、缺失及重排均拒绝。两种方法都复用 P3-04 校验声明依赖，保持收到的步骤顺序。

## P3-07 方案草稿

`geopilot.plans.draft_builder.build_draft(context, method, *, plan_id, version=1, options=None) -> AnalysisPlan` 生成 `draft` 快照，保留所选确切资产、声明的步骤顺序、工具版本和明确配置的参数。缺失角色、上下文问题、未绑定工具或选项、无效值和未绑定成果记录到 `issues`。选项仅来自用户提供或配置声明的默认值，参考选项在条件已选或实际步骤绑定时列为必填。输出目标按步骤、命名端口和格式生成运行目录内的相对引用。草稿可以查看缺项，确认和编译继续复核前提。

## P3-08 参数与步骤请求编译

`geopilot.plans.compiler.compile_plan_steps(plan, method, *, registry)` 返回规范化的有序 `PlanStep` 元组，供确认服务保存。复核方法和工具的确切版本、问题列表、资产角色、锁定顺序、步骤绑定、参数及命名输出。通过 P2-03 将明确选项和字面量转换成契约参数，接受与绑定原值或规范值一致的方案参数。成果目标继续使用方案保存的相对引用。

`compile_step_request(plan, step_key, *, run_id, asset_files, prior_results, workdir_ref=None) -> StepRequest` 接收确认方案，使用运行目录内资产文件 `{role: StepFileRef}` 和已成功的前序 `{step_key: StepResult}`。前序结果须来自同次运行，使用实际报告的命名成果及图层，并复核声明目标与类型。生成精确工具、参数、目标、步骤日志与取消文件引用。 `PlanCompilationError(code, field, message)` 定位不可编译的内容。编译器不创建文件或启动进程。

## P3-09 方案持久化与确认

`geopilot.plans.service.save_draft(database_path, plan, *, context=None)` 在已有 SQLite 表中保存任务和精确草稿版本。 `load_plan(database_path, plan_id, version)` 读取准确版本，缺失时抛出 `LookupError`。保存使用新版本键，确认后原版本不可覆盖。

`confirm_plan(database_path, plan_id, version, *, catalog, registry, registered_rules, precondition_checks=None)` 重查实际来源、方法配置、草稿问题、关键选项及已声明前提，调用 P3-08 固定实际参数。名称映射中的前提函数接收 `(plan, {role: DataAsset})` 并返回 `tuple[PlanIssue, ...]`。有效资产角色与匹配类型由任务上下文核对。确认以 SQLite 事务将同一版本保存为 `confirmed`，可重读精确快照。失败保留草稿并用 `PlanConfirmationError` 携带 `issues`。确认过程不启动计算。

## P3-10 编辑与版本

`geopilot.plans.versioning.revise_plan(database_path, plan_id, version, *, context, method, options=None)` 使用现有任务上下文和目标方法生成候选草稿。输入、关键选项、方法或目标变化时，以新版本保存 `draft`，同一方法省略选项时沿用已选值。目标任务保持一致。同一 SQLite 写事务核对基线和分配版本，重复提交相同草稿可复用该版本，过期基线的不同内容返回版本冲突。历史确认快照及运行引用可按原版本查询。

## P3 阶段运行交接

`geopilot.application.run_inputs.stage_run_inputs(database_path, inputs, workdir)` 重开精确资产版本，将完整文件组复制到该运行的 `inputs`，核对内容指纹并保留选择的图层。相同源文件和指纹在同次运行只复制一份。

阶段同步运行由确认快照、运行目录和 P3-08 单步请求驱动。 `geopilot.application.run_checks` 的规则接收 `RunCheckContext(plan, workdir, asset_files, steps, artifacts)`，返回 `RunCheckResult(passed, message, observed)`。配置声明的规则名映射到相应实现，运行状态与成果检查状态分别记录。

`geopilot.application.plan_runner.run_confirmed_plan(database_path, plan_id, version, *, workspace_root, run_id, registry, ...)` 同步运行精确已确认版本，在 SQLite v1 的 `run` 保存独立状态和目录。保存运行请求、逐步真实日志、实际成果的 `outputs/manifest.json` 与 `checks/report.json`。全部执行成功后才进入检查。规则全部完成且存在不通过判定时记为 `SUCCEEDED / FAILED`。检查中断记为 `SUCCEEDED / NOT_CHECKED`，报告记录 `rules`、`completed_rules`、`pending_rules` 及 `interruption`。SQLite 的 `error_json.observed` 保存完整中断报告，含报告无法写入的情形。确认拒绝空规则配置。P4 接入后台调度和成果表。

## P4 运行创建

`geopilot.runs.service.create_run(database_path, plan_id, version, *, workspace_root) -> str` 读取精确确认版本，复用资产重开和文件组暂存，写入 P3 同形的运行快照。提交 SQLite 后返回 `run_id`，初态为 `NOT_STARTED / NOT_CHECKED`。已有排队、执行或检查中的运行时返回其编号。并发受理在写事务中复核活动记录，清理未发布的候选目录。计算由后台调度启动。

SQLite v2 增加运行控制字段及 `run_step`。 `geopilot.runs.models.Run` 和 `RunStep` 保存运行和零基有序步骤，执行与检查使用独立枚举。 `geopilot.persistence.run_repository` 提供同连接读写，事务由调用方持有。

`geopilot.scheduler.loop.claim_next_run(database_path)` 原子领取并发布 `RUNNING`。 `run_once(database_path, execute)` 和 `run_loop(database_path, execute, stop, *, poll_seconds=0.25)` 调用接收 `Run` 的执行器。 `recover_interrupted_runs(database_path, *, processes_stopped)` 在旧受管进程已验证停止后记录中断，检查中断保留执行成功。

`geopilot.application.plan_runner.execute_claimed_run(database_path, run, *, workspace_root, registry, ...)` 执行实际受理快照。与同步入口共用 `geopilot.scheduler.step_executor.execute_steps`。SQLite 先登记有序待执行步骤，再记录请求、结果、日志和当前步骤；失败时后续保持 `NOT_STARTED`。后台入口调用 `preflight_step_inputs` 写入并验证实际输入规则。

`geopilot.scheduler.output_resolver.resolve_named_output(workdir, output, *, field)` 在消费工具启动前按声明类型和图层重开前序成果，返回相对于运行目录的 `StepFileRef`。文件缺失记为 `OUTPUT_MISSING`，损坏、图层缺失或类型不符记为 `OUTPUT_INVALID`。

`geopilot.api.app.create_app(database_path)` 使用本机已初始化的数据库。 `GET /api/runs/{id}` 从 SQLite 返回 `run_id`、`execution_status`、`check_status`、`current_step`、`cancel_requested`、`error` 和 `current_report_id`。读取存在记录返回 HTTP 200，记录缺失返回 404，存储读取错误返回 500。 `geopilot.runs.query_service.get_run_status` 使用只读 SQLite 连接。

`POST /api/runs/{id}/cancel` 返回同形状态，受理请求为 HTTP 202，已结束为 200。排队任务在 SQLite 内原子取消。运行中先提交 `cancel_requested`，适配器在进程停止后保存 `CANCELLED / PROCESS_CANCELLED`，工具超时保持 `FAILED / PROCESS_TIMEOUT`。调度器按同一数据库控制后续步骤派发。检查取消和单独重查见 P4-16。

`geopilot.scheduler.failure_handler.list_run_diagnostics(database_path, run_id)` 以已记录请求与步骤为证据，返回实际留存的非正式文件角色、类型、相对路径、图层和可读性。缺失文件不列出，存在但无法重开的文件列为不可读。

`retry_run(database_path, previous_run_id, *, workspace_root, plan_version=None)` 默认重试旧运行的确切确认版本，显式版本须为同方案已确认版本。复用创建运行与单活动受理约束；新运行从第一步计算。

`GET /api/plans/{id}?version=1` 读取确切版本。 `POST /api/plans/{id}/confirm` 与 `POST /api/plans/{id}/runs` 都接收 `{"plan_version": 1}`；前者只确认并返回方案，后者返回 HTTP 202 与 `{"run_id": "..."}`。同一次活动运行仍返回原运行编号。

SQLite v3 增加 `artifact`。`prepare_artifacts` 重开整批实际输出并核对版本、类型、文件指纹与来源。 `insert_artifacts` 在调用方事务核对运行与步骤后整批写入，运行同一事务进入 `SUCCEEDED / CHECKING`。 `GET /api/runs/{id}/artifacts` 返回 `run_id`、`check_status`、`artifacts` 和 `diagnostics`；已登记成果即使当前不可读仍保留元数据。

`check_run_process(database_path, run_id, *, registry, qgis_bindings=None, python_implementations=None)` 读取实际请求、结果和引擎证据并对照确认方案，返回按字段定位的类型化过程检查。

`check_run_artifacts(database_path, run_id, *, rules=None)` 验证正式成果完整性、来源、真实文件与指纹，再按已确认的规则名执行配置化检查。结果保存阶段、严重度、观察与期望，`passed=None` 表示未执行。

SQLite v4 增加 `check_report`。 `publish_report(connection, report, *, error=None)` 原子保存规则版本、明细、最终检查状态及当前报告编号。 `GET /api/runs/{id}/checks` 返回 `run_id`、`check_status`、`current_report` 和按新到旧排序的 `report_ids`。真实输入、过程、成果证据保存于 `phase_findings`，规则结果保存于 `rules`。

`POST /api/runs/{id}/checks` 对已成功计算且检查中断的运行受理重查，验证原规则版本与实际正式成果文件组。返回 HTTP 202 和 `report_id`，同运行的待领取或活动重查返回同一编号。领取后再次验证，保留原 GIS 步骤与文件。 `GET /api/runs/{id}/checks?report_id=...` 额外返回 `selected_report`。检查取消与服务中断保留 `SUCCEEDED / NOT_CHECKED`。

`create_app(..., execute_run=...)` 的服务生命周期负责后台领取与退出，独占每个 SQLite 数据库的调度锁。受管本机步骤持有可继承的运行目录进程锁，服务启动核实该锁释放后恢复旧运行。CLI `python -m geopilot.api.server` 载入版本化配置并绑定本机回环接口。

## P6 矢量关系与测量

`spatial-select-and-clip` v1 按 `spatial_relation` 在完整目标上执行 `intersects` 或 `within`，再裁回明确的 `analysis_extent`。`clipped_targets` 为保留非空交集的通用矢量层，`original_targets` 为同 ID 的裁前完整对象。点或线状接触仍保留几何和原 ID。

`split_area_by_zone(targets, zones)` 对裁回的面分摊等积平方米，未覆盖归入 `None`。共享边界不产生面积项，正面积分区重叠触及目标时拒绝重复分摊。

`assign_by_largest_overlap(targets, zones, *, boundary_tie="unassigned")` 在裁前完整面上比较各分区的正等积相交面积，唯一最大者获归属，无重叠或在数值容差内并列返回 `None`。

`polygon-zone-statistics` v1 锁定完整对象判断、裁剪与分区统计。 `allocation_details` 保存逐 ID 明细，`zone_statistics` 保存去重总数、各区统计、物理未覆盖及按规则未归属的面积。空裁剪结果在适用等积 CRS 和分区通过前置校验时生成空明细与零统计。

`python-wgs84-geodesic-buffer` v1 是经验证的二维 WGS84 道路线测地线缓冲契约。适用距离 1–2000 m，支持命名 GeoPackage 输出。统一入口的 `validate_tool_distance` 按精确契约放行，详细几何域和离散阈值见 P6-09 验收记录。既有七步道路 v2 使用局部投影。

`land-use-change` v1 锁定七步并发布 `change_polygons`、`transition_matrix`、`zone_statistics`。两期分类显式映射，质心与最大重叠用裁前交集，面积拆分用裁后等积形状。可选同转移共边合并先于面积阈值。报告核对映射、叠加、来源、矩阵与分区。

## P5 工作台 HTTP 连接

`GET /api/assets` 返回 `{assets}`，每版包含 `asset_id`、`asset_version`、`source`、`source_type`、`metadata`、`availability`、`created_at`。 `POST /api/assets` 接收 `{path, layer?}`，返回同形资产。 `GET /api/assets/layers?path=...` 返回 GeoPackage 的 `{layers: [{name, geometry_type}]}`。

`GET /api/methods` 返回 `{methods: [{title, description, definition, available, issues}]}`，`definition` 是已登记的精确版本和规范化选项定义，`issues` 提供实际工具或规则定位。

`POST /api/tasks` 接收 `{goal: {summary}}`，返回 201 与 `task_id`。 `POST /api/tasks/{id}/context` 接收 `{method: {id, version}, inputs, goal?}` 并返回已保存的精确角色上下文。 `POST /api/tasks/{id}/plans` 使用相同输入以及 `critical_options`，可带成对的 `base_plan_id` 和 `base_plan_version` 生成新版本。 `GET /api/tasks/{id}` 返回任务目标、上下文、方案版本和运行编号列表。

`GET /api/runs/{id}/steps` 返回 `{run_id, steps}`，步骤字段包含 `step_key`、`position`、`status`、`started_at`、`finished_at`、`log_available`、`error`。 `GET /api/runs/{id}/steps/{step_key}/log` 返回实际文本日志，可加 `tail` 只读尾部。

`GET /api/assets/{id}/preview?version=...` 与 `GET /api/artifacts/{id}/preview` 返回本机重开的 WGS84 GeoJSON，保留实际要素属性。 `GET /api/artifacts/{id}/file` 下载该正式编号关联的原始本机文件。预览失败和原始文件访问分别处理。

`GET /api/artifacts/{id}/table?limit=100` 从已登记 CSV 读取 `{columns, rows, truncated}`，限制范围 1–500，原文件下载独立。 `GET /api/runs/{id}/origin` 只读返回 `{run_id, task_id, plan_id, plan_version}`，按运行保存的精确方案键关联。缺失运行返回 404。工作台用任务、方案及运行编号恢复同一记录，恢复流程只读。

## P10 本机脚本结果浏览与 PostGIS 快照

`GET /api/script-runs` 列出工作区 `scripts/` 已发布的脚本快照，单项包括 `script_run_id`、`script_id`、`version`、`created_at`、`status`、`log_available`。 `GET /api/script-runs/{id}` 加退出码、超时、起止时间、错误及输出清单。状态为 `NOT_STARTED`、`RESULT_PENDING`、`SUCCEEDED` 或 `FAILED`。 `GET /api/script-runs/{id}/log` 返回实际日志，`tail` 范围 1–1000。 `GET /api/script-runs/{id}/outputs/{index}/file` 下载原文件，`/preview` 返回支持的单层 GeoPackage 或 GeoJSON 的 WGS84 预览。脚本记录来源为本机脚本工作区，独立于正式任务的 SQLite 运行与成果。

`geopilot.data.postgis_import.refresh_postgis_snapshot(database_path, destination, *, conninfo, source, layer="snapshot")` 对已登记的来源选择重新只读导出，按同一逻辑资产 ID 追加精确版本。每版使用独立目标文件，保存导出时点和文件指纹，正式执行只使用方案引用的本机快照版本。

`geopilot.scripts.registration.validate_manual_registration(database_path, workspace_root, candidate, *, registered_rules)` 只读核对候选工具、方法、成功试跑、精确输入及独立参考，返回 `RegistrationEvidence`。正式启用由维护者操作。

## P9 模型候选边界

`geopilot.llm.context_builder.build_outbound_context(context, assets, *, protected_literals=())` 从确切本机资产版本取结构和统计白名单，返回 `OutboundContext(goal, roles)`。角色包含就绪状态与类型化摘要。可见分辨率独立为标量。用户文字中的已知来源与本机样本引用在构建时置换，复杂原始内容必须先在本机处理。结果须经完整请求的外发边界校验。

`geopilot.llm.base.ModelProvider.generate(request, *, timeout) -> ModelResponse` 使用 `ModelRequest(payload)` 的有限 UTF-8 JSON 与 `ModelResponse(text)` 的原始返回文本。 `ModelProviderError` 只提供固定错误编号、文字与重试属性。Mock provider 为固定、无网络的开发实现。

## P7 栅格窗口与 DEM

`geopilot.gis.raster.window_io.read_raster_window(source, window, *, bands=None)` 读取整数像元窗口，返回掩膜数组、窗口仿射、CRS 和所选波段 NoData。 `copy_raster_window(source, destination, window)` 分块写独立 GeoTIFF，保留有效掩膜和波段元数据。范围须完整位于源网格。仅支持同 NoData 或可单文件表达的掩膜，输出以同文件系统硬链接发布。

`geopilot.llm.prompt_builder.build_model_request(context, catalog, *, registry, registered_rules)` 将白名单上下文、精确可用方法与其工具、步骤和选项约束写入 `ModelRequest.payload`。候选不包含实现、原始文件路径、任意默认值或坐标。

`geopilot.gis.raster.extent_expansion.expand_analysis_window(grid, bounds, radius_pixels)` 将同 CRS 的范围向外对齐北向上像元，再按算法半径外扩。 `ExpandedWindow` 记录请求范围、分析窗、外扩窗和对应网格范围。覆盖不足时拒绝。 `resolve_dem_units(metadata, *, band=1, vertical_unit=None)` 返回来源与垂直相对水平的换算比例。 `python-dem-unit-check` v1 重开 DEM，校对请求中保存的 `z_factor`，发布 JSON 与日志证据。

`parse_model_response(response, *, request)` 检查回复结构、已发布候选版本和枚举，说明文字问题与结构分离。 `validate_model_candidate(...)` 再核对当前本机配置、资产与工具参数，生成尚未持久化的候选草稿及可定位问题。只有既有方案入口能保存、确认或执行。

`check_outbound_request(serialized, *, protected_literals=())` 检查完整 UTF-8 JSON 请求与白名单结构。拒绝时 `DataBoundaryError` 只返回固定错误编号和已知字段位置。

`python-dem-expand-extent` v1 以同水平 CRS 的实际矢量分析区和 DEM 推导 `windows` JSON。 `python-dem-prepare` v1 重开原 DEM 与记录，分块输出 `prepared_dem`，保留网格、完整 CRS、NoData、掩膜与单位。

`GET /api/model-assistance` 返回 `{available}`。 `POST /api/model-assistance` 接收本机目标、可选精确方法、角色资产引用和可选的本机受保护字面值，返回模型候选的 `method`、`goal`、`options`、`missing`、`explanation`、`explanation_issue` 与 `issues`。建议预览不写任务、方案或运行。应用建议仅填手工表单，再走现有草稿与确认入口。

`request_model_suggestion` 使用校验后的一致请求快照，重试 1–3 次已归类的瞬时错误。 `OllamaModelProvider` 为可选本机适配器，调用环回接口与明确的 `:local` 模型来源。 `python -m geopilot.api.server --ollama-model <name>` 启用该供应商，其他启动方式保留手工流程。

`python-dem-aspect` v1 用准备 DEM 与单位报告写 Horn 坡向。网格北为 0°、顺时针；平地或不完整邻域为 NoData。

`python-dem-slope` v1 使用准备 DEM、单位报告及确认的单位比例，输出 Horn 坡度的度或百分比。共享 Horn 按真实横纵像元间距和波段尺度分块计算，不插值外边缘或不完整邻域。

`python-dem-terrain` v1 让固定步骤 `calculate_slope_or_aspect` 在相同 DEM、单位证据和输出端口下选择已登记的坡度或坡向计算，复用两套真实工具。

`python-dem-clip-back` v1 核对外扩网格和原分析区引用，按原网格裁回并按原多边形覆盖的像元中心写掩膜。保留实际像元、仿射、CRS、NoData 和 Horn 标签。

`python-dem-threshold` v1 以 `{intervals: [{class_id, lower, upper, include_lower, include_upper}]}` 分类有效值。无界端可用 `null`，0 表示有效未选，1–254 为选中类别，255 为 NoData。保留原网格并记录阈值标签。

`python-dem-area-stats` v1 从真实 `threshold_regions` 统计选中类别，在显式等积 `area_crs` 下量原像元完整边界。面积单位为平方米，无重采样。P4 前置和运行器校验同一实际目标及范围。

### P7-10 正式 DEM 方法与栅格成果

`dem-terrain-analysis@1` 固定附录 A.2 的七步，正式成果为裁回的 `terrain_raster`、`threshold_regions` 和 `area_statistics`。`GET /api/methods` 的 `derived_options` 列出由本机计算的选项。草稿服务按确切 DEM 波段和垂直单位推导 `z_factor`，工作台与模型候选只接收用户可编辑的选项。确认重查单位、邻域覆盖、阈值区间和面积投影。

`read_raster_artifact_metadata(path)` 保留原 CRS、仿射、网格、分辨率、范围、类型与 NoData，增加每波段 `band_statistics` 和交集的 `common_valid_pixel_count`。摘要通过原存储值和原掩膜分块读取，分别登记有限有效、掩膜及非有限像元数量，记录有效像元边界和最小值、最大值、均值。无有效实数样本时对应值为 `null`。

`dem_covers_expanded_extent`、`units_resolved`、`border_pixels_match_reference` 以 P4 成果规则登记。以暂存的确切原 DEM 和分析区复核邻域、单位记录、裁回的网格、边缘和内部有效像元及掩膜，并核对阈值分类与面积摘要。执行后和检查中断后的重查共用同一规则版本。

### P7-11 正式栅格的地图预览

`GET /api/artifacts/{id}/raster-preview?palette=terrain&max_size=1024&band=1` 返回 `artifact_id`、`source_fingerprint`、`image_url`、`coordinates`、`width`、`height`、`band`、`palette`、`color_mode`、`source_crs`、`source_resolution`、`display_crs`、`display_resolution`、`resampling`、`ramp`、`stretch`、`classes` 和 `nodata_alpha`。最大边长可选 1–1024，配色可选 `terrain` 或 `grayscale`，默认第一波段。MapLibre 四角是左上、右上、右下、左下的 WGS84 经纬度。预览图使用 EPSG:3857 和最近邻，透明 NoData 的 Alpha 为 0。连续配色记录 `ramp` 和 `stretch`，分类配色记录 `classes`。分辨率以各自 CRS 的 XY 单位记录。

`GET /api/artifacts/{id}/raster-preview.png` 返回同源 RGBA PNG。描述中的 `image_url` 固定正式成果 ID、原始指纹和预览参数。MapLibre 图层以成果 ID 与指纹关联原 TIFF。每个渲染变体在该运行目录的 `previews/<artifact-id>/<variant>/` 中独立保存 `preview.json` 与按 SHA-256 命名的 PNG。变体键包含原指纹、配色、尺寸上限、波段和渲染版本。正式成果定位并通过原始指纹检查后可重开已登记变体。原 TIFF 继续经原成果文件入口读取。

### P7-12 正式栅格原像元查询

`GET /api/artifacts/{id}/value?longitude=…&latitude=…&band=1&source_fingerprint=…` 以 WGS84 经纬度和确切正式成果为输入。可选的指纹在地图调用中随成果 ID 一起传入。服务重开正式 TIFF，核对原文件组，转换到源 CRS，按原仿射网格定位零基行列，只读取命中的一个像元及其 GDAL 有效掩膜。

返回 `artifact_id`、`source_fingerprint`、`band`、`longitude`、`latitude`、`source_crs`、`source_x`、`source_y`、`row`、`column`、`status`、`nodata`、`value`、`dtype`、`unit`、`scale` 和 `offset`。`status` 为 `value`、`nodata` 或 `outside`。有效值为原存储值，超出 JavaScript 安全整数的整数用十进制字符串保留精度。NoData 有行列，值为 `null`。范围外行、列和值均为 `null`。配色、显示像元和重采样参数不参与查询。

## P8 遥感产品配置

### P8-01 版本化产品配置

`SensorProductKey(sensor, product_level, processing_version)` 为精确查找键。`SensorConfigRef(id, version)` 为配置版本引用。`SensorProductConfig` 包含键和引用的各字段，以及 `band_roles`、`scale_offset`、`quality_rules` 和 `source_reference`。提供 `.key` 与 `.ref` 作为对应类型的投影。

波段角色记录 `code`、`resolution_m` 和 `nodata_dn`。必需光谱角色为 `red`、`nir`，按产品使用 `quality` 及 `saturation`。光谱定标声明元数据字段位置。线性定标用 `multiply_add` 和 `scale_field`、`offset_field`。量化定标用 `quantification` 和 `quantification_field`、`dn_offset_field`，后者含按产品元数据定义的零基 `band_id`。字段位置从元数据根开始，以路径数组表示。

位掩膜声明 `bit_width` 与按原因命名的 `invalid_bits`。分类掩膜声明完整类别字典 `classes` 与明确的 `valid_values`。来源记录 `title`、`url`、`updated_at`。Landsat 8/9 OLI/TIRS 配置保留真实 L2SP 或 L2SR 级别与 Collection 字符串 `02`。Sentinel-2 MSI 保留 L2A 与处理基线 `05.11`，光谱近红外使用 10 米 B08。

### P8-02 唯一查找

`load_sensor_registry(directory)` 读取目录的严格 YAML，返回独立的 `SensorConfigRegistry` 快照。同一配置 `id/version` 重复或配置内容无效时抛出带来源路径的 `SensorConfigLoadError`。`lookup(SensorProductKey)` 只匹配完整键。零命中返回 `SENSOR_CONFIG_NOT_FOUND`，多命中返回 `SENSOR_CONFIG_AMBIGUOUS`，两者由 `SensorConfigLookupError` 提供精确 `key` 和有序 `matches`。已保存配置引用可用 `get(SensorConfigRef)` 回取同一版本，缺失返回 `KeyError`。查找和回取返回隔离副本。

### P8-03 产品元数据与波段角色

产品资产为保留原 DN 的 GeoTIFF。`GEOPILOT_PRODUCT` 域保存 `PROFILE`、`METADATA_XML`，Sentinel-2 可用 `GRANULE_ID` 精确选择。当前 profile 为 `landsat-c2-level-2` 与 `sentinel-2-level-2a`。栅格各波段的描述使用明确的产品代码，共同掩膜与光谱填充值分别处理。Sentinel-2 的 SCL 源为 20 米，若进入 10 米共同栈，质量波段的 `GEOPILOT_PRODUCT.RESAMPLING` 记录 `nearest`。

`read_product_metadata(path)` 从实际资产读得 `ProductMetadata`，含 `key`、`profile`、`product_id`、`spacecraft`、原处理级别、XML、granule 和波段来源。`resolve_metadata_field(metadata, field)` 读取精确路径，可按产品的零基 `band_id` 选重复项。`map_product_bands(path, registry=...)` 返回元数据、唯一配置和各角色的 `MappedBand`。波段记录含一基 `index`、产品 `code`、源文件名、原生分辨率、`nodata_dn` 和 `dtype`。缺项、冲突和错误波段均阻断。

`parse_product_metadata(xml, profile=..., granule_id=None)` 支持在打包前读取原生 XML，`band_sources[code]` 保留声明与可打开的原生文件名。映射要求投影米制原光谱网格、保持 DN 的无符号整数和恒等 GDAL 缩放。光谱填充 DN 与合法 QA 0 分别解释，因此共栈全局 NoData 0 会被拒绝。

### P8-04 逐波段反射率

`scale_product_reflectance(source, destination, registry=...)` 写入与原 DN 一致的网格，两个 Float64 波段依次为 `red` 和 `nir`，标记为无量纲 `reflectance`。原 GDAL 无效掩膜和各自的光谱填充 DN 用 NaN 表示。有效反射率 0 保留。线性定标为 `DN * multiplier + offset`，量化定标按 `(DN + offset_DN) / quantification` 的运算顺序。实际系数、来源路径、波段索引和配置引用进入处理证据。`resolve_reflectance_coefficients(mapping)` 提供相同的元数据解析供方法确认时复用。

### P8-05 质量掩膜

`write_quality_mask(source, destination, registry=...)` 按同一产品映射组合原 GDAL 掩膜、光谱填充 DN 及声明的质量位或 SCL 类别。输出原网格 UInt8 单波段，1 表示有效，0 表示无效，两值均为可解释的存储值。结果记录配置引用及有效、无效和未知类别计数。SCL 消费明确最近邻的打包质量波段。

### P8-06 原网格 NDVI

`write_ndvi(reflectance, quality_mask, destination)` 消费精确同格的 `red/nir` Float64 反射率和 UInt8 质量掩膜。按 `(nir - red) / (nir + red)` 计算，零分母、输入无效和非有限算术结果以 NaN 写入。输出为原网格单带 Float64 `ndvi`，单位 `1`。有限范围异常保留在原指数中供检查，并记录计数、范围与样点；参与变化的时相对齐排除超出 `[-1, 1]` 的异常像元。

### P8-07 双时相网格对齐

`align_ndvi(first, second, destination, resampling=..., analysis_extent=None, analysis_layer=None)` 以第一期完整网格为目标。按已确认的 `nearest` 或 `bilinear` 将第二期对齐，先按原掩膜、NoData、有限性及 NDVI 合法范围清洗源像元。实际同格时直接读取。分析面投到目标 CRS 后按像元中心判定，面内边界纳入，孔内排除。

结果为 Float64 双波段 `first_ndvi` 和 `second_ndvi`，单位 `1`，NaN 与内部共掩膜记录两期共同有效范围。`GEOPILOT_NDVI_ALIGNMENT.SUMMARY` 记录 `first_grid`、`second_grid`、`target_grid`、`resampling`、`warped`、`analysis_bounds`、`analysis_pixels`、两期及共同有效数，以及共同有效输出的两期最值范围。网格记录 CRS、六项仿射、分辨率、宽高、范围及 NoData。

### P8-08 共同有效范围的 NDVI 差值

`write_ndvi_change(aligned, destination)` 校验实际双期对齐的两个 Float64 波段、原掩膜与 NaN 一致、网格及合法 NDVI 范围。差值固定为第二期减第一期，输出原格单带 Float64 `ndvi_change`、单位 `1`、NaN NoData。有效的零表示不变。`GEOPILOT_NDVI_CHANGE.SUMMARY` 保存 `direction=second_minus_first`、总量、共同有效、NoData、正负零计数及最值。

### P8-09 变化类别和等积面积

`classify_ndvi_change(source, destination, threshold=..., area_crs=...)` 消费原网格差值。阈值为 0–2，差值小于负阈值为 1 `decrease`，正负阈值之间含端点为 2 `stable`，大于正阈值为 3 `increase`。NoData 为 255。分类在原网格写入，按原像元边界转等积 CRS 测量。统计含阈值、方向、三个类别各自的像元数和面积，未出现的类别保存为 0。

### P8-10 正式两期遥感方法

`remote-sensing-index-change@1` 锁定读取产品、查找配置、映射波段、定标、质量掩膜、NDVI、网格对齐和变化八步。输入角色为 `first_product`、`second_product`、`analysis_extent`。用户确认 `resampling`、`change_threshold`、`area_crs`。草稿和确认服务按确切资产重新读取产品，保存 `first_configuration`、`second_configuration` 完整配置快照。处理步骤使用已保存的快照校对真实元数据和源文件指纹。未知产品配置在形成可执行计划前给出明确错误。

正式成果为 `sensor_configuration`、`calibration_report`、两期质量掩膜和 NDVI、`alignment_report`、`change_raster`、`change_classes` 和 `area_statistics`。前四阶段的 JSON 以 `first_product`、`second_product` 对应期别。定标、掩膜与 NDVI 的 `GEOPILOT_REMOTE_SENSING.SOURCE` 关联期别、原产品及运行内源指纹。四条检查覆盖配置及实际系数、共同有效网格、差值和分类统计、原指数范围。范围异常可被单独报告为警告。

工具契约声明 `area_projected_from_raster_cells` 时，实际测量使用确认的 `area_crs`。该声明要求等积测量策略、一个栅格输入和字符串目标 CRS 参数。

## V2.0 追踪矩阵补齐

### 本机资产上下文

`POST /api/tasks/context-preview` 接收 `goal`、精确 `method`、当前 `inputs`，以及可选的 `critical_options` 和 `criteria`。该入口只计算预览，正式任务上下文仍通过 `POST /api/tasks/{id}/context` 和草稿入口保存。预览、保存和草稿共用同一评估逻辑。

`criteria.scope` 为已登记的范围资产版本。`criteria.object_types` 按方法输入角色选择 `vector_point`、`vector_line` 或 `vector_polygon`。`criteria.expected_artifact` 为方法声明的成果角色，候选角色沿该成果绑定的步骤依赖回溯。固定方法的必需角色继续参与方案验证。候选结果按角色返回 `candidates`，当前显式选择在 `roles` 保留精确资产引用、适用状态和理由。省略新字段的旧上下文仍可读取。

方法可用 `input_context` 声明各输入角色的 `required_fields`、引用字符串选项的 `field_options` 和空间 `scopes`。空间范围可引用方法另一个角色或任务范围，可绑定填写的距离和距离 CRS。核对实际矢量几何或栅格仿射边界，已知 CRS 不同时在同一 CRS 比较。已知不相交会排除候选；声明允许空结果的角色仍可保留显式选择并显示原因。候选范围用于资产筛选，实际裁剪范围仍由方法的输入和选项决定。

### 工具栅格空值前提

工具可按栅格输入端口声明 `raster_nodata`。用 `bands` 指定 `all` 或一组一基波段，或用 `band_parameter` 引用规范化的整数参数。每个波段可允许缺失样本（`allow`）、至少需要一个有效样本（`require_valid_samples`）或拒绝任何无效样本（`reject_invalid_samples`）。`nodata_declaration` 可要求 `none` 或 `nan`，默认 `any`。

执行适配器派发前检查实际 GDAL 波段掩膜和原始有限性。检查按固定大小窗口读取，合法零值按实际掩膜保留。阻断记录定位到 `inputs.<role>.bands.<band>.nodata`。未声明策略的旧工具按原行为执行。

### 工具成果规则

确认时按精确工具版本将工具检查器名称和版本保存到该步骤的 `tool_check_rules`。已确认快照无该字段时继续按其保存的检查规则运行。规则注册表按 `(name, version)` 解析。不存在或同一步重复声明的规则在确认前定位。

正式成果登记后，逐步检查该步骤实际声明的输入与输出，包含进入后续步骤的中间产物。检查报告在 `tool_rules` 和成果阶段明细保存步骤、工具版本、规则版本和判定；方法级规则保留在 `rules`。两个步骤引用同名规则时各自产生记录。中断后的重查沿用同一确认版、原规则版本及原始文件指纹，执行和检查仍各自记录状态。

## 道路缓冲统计 v3

`road-buffer-statistics@3` 沿用七个锁定步骤键和三个正式成果角色。`critical_options.distance_method` 取 `local_projected` 或 `geodesic`。局部投影按 v2 的精确工具和连接运行，要求填写 `distance_crs_ref`。测地线模式使用 WGS84、距离单位为米，工作台不提交局部投影 CRS。工具的距离范围为 1–2000 m。

方法的可选 `step_variants` 以 `option` 指向已声明的必填枚举。`choices` 按枚举值和步骤键绑定精确工具版本，必要时替换该步的输入、参数及输出。草稿和确认解析同一分支，确认快照保存具体工具和参数。原有确认版本按各自保存的步骤运行。

测地线外扩首先准备 WGS84 分析区、目标与分区。候选范围是原分析区及各边界的保守椭球距离包络，包含取数余量；道路筛选在同一 WGS84 坐标域按道路的最短测地弧判断，保留候选道路原始几何与来源标识。准备后的道路进入已经通过 T04 的 `python-wgs84-geodesic-buffer@1`，使用确认的原距离。后续仍先判断目标空间关系，再裁回准备后的分析区，最后按原始对象 ID 去重统计。两种模式均保存正式缓冲、选中目标和分区 CSV。
