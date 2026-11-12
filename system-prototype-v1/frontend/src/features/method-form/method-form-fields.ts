import type { AssetEntry, Json, MethodEntry, OptionSpec } from '../../api/workbench'

export const roleLabels: Record<string, string> = {
  analysis_extent: '分析范围', relation_area: '关系范围', roads: '道路', targets: '目标对象',
  zones: '统计分区', features: '筛选要素', first_classes: '第一期地类', second_classes: '第二期地类', dem: '高程栅格（DEM）',
  first_product: '第一期产品', second_product: '第二期产品',
  network: '已节点化路网', start: '起点', end: '终点',
}

export const optionLabels: Record<string, string> = {
  distance_m: '缓冲距离', distance_method: '距离计算方式', distance_crs_ref: '距离坐标参考系',
  area_crs: '面积坐标参考系', area_crs_ref: '面积坐标参考系', spatial_relation: '空间关系',
  boundary_assignment: '跨界归属', boundary_tie: '边界并列处理', statistic: '统计量', output_scope: '输出范围',
  field_name: '字段名称', match_value: '匹配值', first_class_field: '第一期分类字段',
  second_class_field: '第二期分类字段', first_class_mapping: '第一期分类映射',
  second_class_mapping: '第二期分类映射', first_period: '第一期名称', second_period: '第二期名称',
  terrain_output: '地形指标', slope_unit: '坡度单位', vertical_unit: '高程单位', band: '高程波段',
  threshold: '分类阈值', change_threshold: 'NDVI 变化阈值', resampling: '重采样方式',
  overlay_area_tolerance: '叠加面积容差', minimum_change_area: '最小变化面积', merge_adjacent_classes: '合并相邻同类图斑',
  target_crs: '米制投影坐标参考系', snap_tolerance_m: '最大吸附距离',
}

export const enumLabels: Record<string, string> = {
  local_projected: '局部投影', geodesic: '测地线', intersects: '相交', within: '内部包含', centroid: '质心归属',
  split_by_area: '按面积分摊', largest_overlap: '最大重叠', unassigned: '未归属', count: '计数',
  clipped_to_analysis_extent: '裁回分析范围', slope: '坡度', aspect: '坡向', degree: '度', percent: '百分比',
  metadata: '从 DEM 元数据读取', meter: '米', kilometer: '千米', centimeter: '厘米', millimeter: '毫米',
  foot: '英尺', us_survey_foot: '美国测量英尺', nearest: '最近邻', bilinear: '双线性',
}

export const unitLabels: Record<string, string> = {
  meter: 'm', kilometer: 'km', centimeter: 'cm', millimeter: 'mm', square_meter: 'm²',
  square_kilometer: 'km²', hectare: 'ha', degree: '°', percent: '%',
}

export function assetMatches(asset: AssetEntry, expected: string) {
  if (asset.availability !== 'AVAILABLE') return false
  const { kind, geometry_type: geometry } = asset.metadata
  if (kind === 'vector') {
    return expected === 'vector' || (expected.startsWith('vector_') &&
      expected.slice('vector_'.length).split('_or_').some(type => geometry?.toLowerCase().includes(type)))
  }
  if (kind === 'raster') return expected === 'raster' || expected.startsWith('raster_')
  return expected === 'table'
}

export interface OptionField {
  name: string
  spec: OptionSpec
  value: Json | undefined
  required: boolean
}

export function optionFields(definition: MethodEntry['definition'], values: Readonly<Partial<Record<string, Json>>>): OptionField[] {
  const effective = Object.entries(definition.critical_options).map(([name, declared]) => {
    const spec: OptionSpec = typeof declared === 'string' ? { type: 'json' } : declared
    const supplied = values[name]
    return { name, spec, value: supplied === undefined ? spec.default ?? undefined : supplied }
  })
  const conditions = new Set(effective.flatMap(({ spec, value }) =>
    spec.type !== 'reference' && typeof value === 'string' ? [value] : [],
  ))
  const variants = definition.step_variants
  const choice = effective.find(field => field.name === variants?.option)?.value
  const activeSteps = typeof choice === 'string' ? variants?.choices[choice] : undefined
  const bound = new Set(definition.steps.flatMap(step =>
    Object.values(activeSteps?.[step.key]?.parameters ?? step.parameters ?? {}).flatMap(binding =>
      'option' in binding ? [binding.option] : [],
    ),
  ))
  return effective.flatMap(field => {
    const { name, spec } = field
    if (spec.type === 'reference' && spec.required_for && !conditions.has(spec.required_for) && !bound.has(name)) return []
    return [{ ...field, required: spec.type === 'reference' || spec.required !== false || bound.has(name) }]
  })
}

export function quantityParts(value: Json | undefined) {
  if (value && typeof value === 'object' && !Array.isArray(value) && Object.keys(value).length === 2 &&
      typeof value.value === 'number' && typeof value.unit === 'string' && value.unit) {
    return { value: value.value, unit: value.unit }
  }
  return null
}

export function optionProblem({ spec, value }: OptionField): string | undefined {
  if (value === undefined) return
  if (spec.type === 'number' || spec.type === 'integer') {
    const quantity = spec.unit ? quantityParts(value) : null
    const numeric = quantity?.value ?? value
    if (typeof numeric !== 'number' || !Number.isFinite(numeric)) return '请输入有限数值'
    // Other units are checked against the declared bounds after conversion on the server.
    if (quantity && quantity.unit !== spec.unit) return
    if (spec.type === 'integer' && !Number.isInteger(numeric)) return '请输入整数'
    if (spec.minimum != null && numeric < spec.minimum) return `数值至少为 ${spec.minimum}`
    if (spec.maximum != null && numeric > spec.maximum) return `数值至多为 ${spec.maximum}`
    if (spec.minimum_exclusive != null && numeric <= spec.minimum_exclusive) return `数值须大于 ${spec.minimum_exclusive}`
    if (spec.maximum_exclusive != null && numeric >= spec.maximum_exclusive) return `数值须小于 ${spec.maximum_exclusive}`
  }
  if (spec.type === 'enum' && (typeof value !== 'string' || !spec.values?.includes(value))) return '请选择定义中的值'
  if (spec.type === 'boolean' && typeof value !== 'boolean') return '请选择是或否'
  if (spec.type === 'string' && typeof value !== 'string') return '请输入文本'
  if (spec.type === 'reference' && (typeof value !== 'string' || !value.trim())) return '请输入引用编号'
}

export function parseOptionJson(text: string): Json {
  return JSON.parse(text, (_key, value: unknown) => {
    if (typeof value === 'number' && !Number.isFinite(value)) throw new RangeError('Nonfinite JSON number')
    return value
  })
}
