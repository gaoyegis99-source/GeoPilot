import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { useState } from 'react'
import { afterEach, expect, it, vi } from 'vitest'
import type { AssetEntry, AssetRef, ContextCriteria, Json, MethodEntry, TaskContext } from '../../api/workbench'
import { MethodForm, type MethodFormProps, type MethodFormRequest } from './MethodForm'

afterEach(cleanup)

const road: MethodEntry = {
  title: '道路缓冲统计', description: '', available: true, issues: [],
  definition: {
    id: 'road-buffer-statistics', version: 2, locked_order: true, steps: [], check_rules: [],
    inputs: { roads: 'vector_line', targets: 'vector_point' },
    critical_options: {
      distance_m: { type: 'number', unit: 'meter', minimum_exclusive: 0 },
      distance_method: { type: 'enum', values: ['local_projected', 'geodesic'] },
      distance_crs_ref: { type: 'reference', required_for: 'local_projected' },
      spatial_relation: { type: 'enum', values: ['intersects', 'within'], default: 'intersects' },
    },
  },
}

const filter: MethodEntry = {
  title: '属性筛选与导出', description: '', available: true, issues: [],
  definition: {
    id: 'attribute-filter-export', version: 1, locked_order: true, steps: [], check_rules: [],
    inputs: { features: 'vector_point' },
    critical_options: { field_name: { type: 'string' }, match_value: { type: 'string' } },
  },
}

function asset(id: string, version: number, geometry: string, availability: AssetEntry['availability'] = 'AVAILABLE'): AssetEntry {
  return {
    asset_id: id, asset_version: version, source: { path: '/fixtures/road.gpkg', layer: id },
    source_type: 'local_file', availability, created_at: '2026-11-11T00:00:00Z',
    metadata: { kind: 'vector', format: 'GPKG', geometry_type: geometry },
  }
}

const assets = [asset('roads', 2, 'MultiLineString Z'), asset('buildings', 1, 'Point'), asset('missing', 1, 'Point', 'UNAVAILABLE')]
const roadRef = { asset_id: 'roads', asset_version: 2 }
const pointRef = { asset_id: 'buildings', asset_version: 1 }

function ControlledForm({ method = road, context = null, onPrepare, initialOptions = {} }: {
  method?: MethodEntry; context?: TaskContext | null; onPrepare: MethodFormProps['onPrepare']; initialOptions?: Record<string, Json>
}) {
  const [inputs, setInputs] = useState<Record<string, AssetRef | undefined>>({})
  const [options, setOptions] = useState<Record<string, Json | undefined>>(initialOptions)
  const [goal, setGoal] = useState('统计道路范围')
  return <MethodForm {...{ method, assets, inputs, options, goal, context, onPrepare }}
    onGoal={setGoal}
    onInput={(role, value) => setInputs(previous => ({ ...previous, [role]: value ?? undefined }))}
    onOption={(name, value) => setOptions(previous => ({ ...previous, [name]: value }))}
  />
}

function choose(label: string, value: string) {
  fireEvent.change(screen.getByLabelText(label), { target: { value } })
}

function choices(label: string) {
  return Array.from(screen.getByLabelText<HTMLSelectElement>(label).options, item => item.value)
}

it('limits roles to compatible available versions and submits numeric values in the declared unit', () => {
  const onPrepare = vi.fn()
  render(<ControlledForm onPrepare={onPrepare} />)
  expect(screen.getByLabelText('任务目标').getAttribute('name')).toBe('goal.summary')
  expect(screen.getByLabelText('道路').getAttribute('name')).toBe('inputs.roads')
  expect(screen.getByLabelText('缓冲距离').getAttribute('name')).toBe('critical_options.distance_m')
  expect(choices('道路')).toEqual(['', 'roads@2'])
  expect(choices('目标对象')).toEqual(['', 'buildings@1'])
  expect(screen.getByText('m')).toBeTruthy()
  expect(screen.queryByLabelText('距离坐标参考系')).toBeNull()
  choose('道路', 'roads@2')
  choose('目标对象', 'buildings@1')
  choose('缓冲距离', '500.5')
  choose('距离计算方式', 'local_projected')
  expect(screen.getByLabelText<HTMLInputElement>('距离坐标参考系').required).toBe(true)
  choose('距离坐标参考系', 'EPSG:32650')
  fireEvent.click(screen.getByRole('button', { name: '生成草稿' }))
  expect(onPrepare).toHaveBeenCalledExactlyOnceWith({
    method: { id: 'road-buffer-statistics', version: 2 }, goal: { summary: '统计道路范围' },
    inputs: { roads: roadRef, targets: pointRef },
    critical_options: { distance_m: 500.5, distance_method: 'local_projected', distance_crs_ref: 'EPSG:32650', spatial_relation: 'intersects' },
  })
  choose('距离计算方式', 'geodesic')
  expect(screen.queryByLabelText('距离坐标参考系')).toBeNull()
  fireEvent.click(screen.getByRole('button', { name: '生成草稿' }))
  expect(onPrepare.mock.lastCall?.[0].critical_options).toEqual({ distance_m: 500.5, distance_method: 'geodesic', spatial_relation: 'intersects' })
})

it('derives required parameters from the selected tool variant', () => {
  const onPrepare = vi.fn()
  const variantMethod: MethodEntry = {
    ...road,
    definition: {
      ...road.definition, version: 3,
      critical_options: {
        ...road.definition.critical_options,
        audit_tag: { type: 'string', required: false },
      },
      steps: [{ key: 'prepare', tool: null }],
      step_variants: {
        option: 'distance_method',
        choices: {
          local_projected: { prepare: {
            tool: { id: 'python-prepare-measurement-lines', version: 1 },
            parameters: { target_crs: { option: 'distance_crs_ref' }, tag: { option: 'audit_tag' } },
          } },
          geodesic: { prepare: {
            tool: { id: 'python-prepare-wgs84-measurement-lines', version: 1 },
            parameters: { target_crs: { literal: 'EPSG:4326' } },
          } },
        },
      },
    },
  }
  render(<ControlledForm method={variantMethod} onPrepare={onPrepare} />)
  choose('道路', 'roads@2')
  choose('目标对象', 'buildings@1')
  choose('缓冲距离', '500')
  choose('距离计算方式', 'local_projected')
  expect(screen.getByLabelText<HTMLInputElement>('audit_tag').required).toBe(true)
  choose('audit_tag', 'survey')
  choose('距离坐标参考系', 'EPSG:32650')
  fireEvent.click(screen.getByRole('button', { name: '生成草稿' }))
  expect(onPrepare.mock.lastCall?.[0].critical_options).toMatchObject({
    distance_method: 'local_projected', distance_crs_ref: 'EPSG:32650', audit_tag: 'survey',
  })

  choose('距离计算方式', 'geodesic')
  expect(screen.getByLabelText<HTMLInputElement>('audit_tag').required).toBe(false)
  expect(screen.queryByLabelText('距离坐标参考系')).toBeNull()
  fireEvent.click(screen.getByRole('button', { name: '生成草稿' }))
  expect(onPrepare.mock.lastCall?.[0].critical_options).not.toHaveProperty('distance_crs_ref')
})

it('uses a new method definition and omits stale option keys on switch', () => {
  const onPrepare = vi.fn()
  const { rerender } = render(<ControlledForm onPrepare={onPrepare} initialOptions={{ distance_m: 600 }} />)
  choose('道路', 'roads@2')
  rerender(<ControlledForm method={filter} onPrepare={onPrepare} />)
  expect(screen.queryByLabelText('缓冲距离')).toBeNull()
  expect(screen.queryByLabelText('道路')).toBeNull()
  expect(choices('筛选要素')).toEqual(['', 'buildings@1'])
  choose('筛选要素', 'buildings@1')
  choose('字段名称', 'source_feature_id')
  choose('匹配值', 'building-west')
  fireEvent.click(screen.getByRole('button', { name: '生成草稿' }))
  expect(onPrepare).toHaveBeenCalledExactlyOnceWith({
    method: { id: 'attribute-filter-export', version: 1 }, goal: { summary: '统计道路范围' },
    inputs: { features: pointRef }, critical_options: { field_name: 'source_feature_id', match_value: 'building-west' },
  })
})

it('keeps boolean, integer and JSON types and prevents invalid inputs from submitting', () => {
  const method: MethodEntry = {
    ...filter, definition: { ...filter.definition, critical_options: {
      limit: { type: 'integer', minimum: 0, maximum_exclusive: 5 },
      categories: { type: 'json' }, enabled: { type: 'boolean', required: false, default: false },
      threshold: 'user_defined',
    } },
  }
  const onPrepare = vi.fn()
  render(<ControlledForm method={method} onPrepare={onPrepare} />)
  choose('limit', '5')
  const submit = screen.getByRole<HTMLButtonElement>('button', { name: '生成草稿' })
  expect(submit.disabled).toBe(true)
  choose('limit', '2')
  choose('categories', '{"x":1e999}')
  expect(screen.getByText('请输入有效的 JSON 值')).toBeTruthy()
  expect(submit.disabled).toBe(true)
  choose('categories', '{"1":"林地"}')
  choose('分类阈值', 'null')
  expect(submit.disabled).toBe(false)
  fireEvent.click(submit)
  expect(onPrepare.mock.lastCall?.[0].critical_options).toEqual({ limit: 2, categories: { '1': '林地' }, enabled: false, threshold: null })
})

it('keeps a restored explicit quantity and shows only matching server role evidence', () => {
  const context: TaskContext = {
    task_id: 'task', goal: { summary: '统计道路范围' }, method: { id: 'road-buffer-statistics', version: 2 },
    roles: { roads: { asset: roadRef, status: 'CONDITIONAL', code: 'CHECK_CRS', message: '需要米制投影' } },
  }
  const onPrepare = vi.fn()
  render(<ControlledForm context={context} onPrepare={onPrepare} initialOptions={{ distance_m: { value: 0.5, unit: 'kilometer' } }} />)
  expect(screen.queryByText('需要米制投影')).toBeNull()
  choose('道路', 'roads@2')
  expect(screen.getByText('需要米制投影')).toBeTruthy()
  expect(screen.getByText('km')).toBeTruthy()
  expect(screen.getByLabelText<HTMLInputElement>('缓冲距离').value).toBe('0.5')
  choose('缓冲距离', '0.75')
  fireEvent.click(screen.getByRole('button', { name: '生成草稿' }))
  expect(onPrepare.mock.lastCall?.[0].critical_options.distance_m).toEqual({ value: 0.75, unit: 'kilometer' })
  choose('道路', '')
  expect(screen.queryByText('需要米制投影')).toBeNull()
})

it('allows a partial draft only after the goal is supplied', () => {
  const onPrepare = vi.fn()
  render(<ControlledForm method={filter} onPrepare={onPrepare} />)
  choose('任务目标', '  ')
  const submit = screen.getByRole<HTMLButtonElement>('button', { name: '生成草稿' })
  expect(submit.disabled).toBe(true)
  choose('任务目标', '筛选指定目标')
  expect(screen.getByText('待选择')).toBeTruthy()
  expect(screen.getAllByText('待填写')).toHaveLength(2)
  expect(submit.disabled).toBe(false)
  fireEvent.click(submit)
  expect(onPrepare).toHaveBeenCalledExactlyOnceWith({
    method: { id: 'attribute-filter-export', version: 1 }, goal: { summary: '筛选指定目标' }, inputs: {}, critical_options: {},
  })
})

it('refreshes the scoped shortlist and preserves a selected asset with its concrete diagnosis', async () => {
  const outside = { asset_id: 'outside', asset_version: 1 }
  const scope = { asset_id: 'scope', asset_version: 1 }
  const available = [...assets, asset('outside', 1, 'Point'), asset('unrelated', 1, 'Point'), asset('scope', 1, 'Polygon')]
  const method: MethodEntry = {
    ...filter, definition: { ...filter.definition, expected_artifacts: ['filtered_features'] },
  }
  const onPrepare = vi.fn()
  const previewContext = vi.fn(async (request: MethodFormRequest): Promise<TaskContext> => ({
    task_id: 'preview', method: request.method, goal: request.goal, criteria: request.criteria,
    roles: { features: {
      asset: request.inputs.features,
      status: request.criteria?.scope ? 'BLOCKED' : 'READY',
      code: request.criteria?.scope ? 'INPUT_SCOPE_DISJOINT' : null,
      message: request.criteria?.scope ? '资产与候选范围不交叠' : null,
    } },
    candidates: { features: [{ asset: pointRef, status: 'READY', code: null, message: null }] },
  }))

  function ScopedForm() {
    const [criteria, setCriteria] = useState<ContextCriteria>({})
    return <MethodForm method={method} assets={available} goal="筛选指定目标" inputs={{ features: outside }}
      options={{ field_name: 'class_code', match_value: 'A' }} context={null}
      criteria={criteria} onCriteria={setCriteria} previewContext={previewContext}
      onGoal={vi.fn()} onInput={vi.fn()} onOption={vi.fn()} onPrepare={onPrepare} />
  }

  render(<ScopedForm />)
  choose('候选筛选范围', 'scope@1')
  choose('预期成果（候选筛选）', 'filtered_features')
  await waitFor(() => expect(screen.getByText('资产与候选范围不交叠')).toBeTruthy())
  expect(choices('筛选要素')).toEqual(['', 'outside@1', 'buildings@1'])
  expect(screen.getByLabelText<HTMLSelectElement>('筛选要素').value).toBe('outside@1')
  expect(previewContext.mock.lastCall?.[0].criteria).toEqual({ scope, expected_artifact: 'filtered_features' })
  fireEvent.click(screen.getByRole('button', { name: '生成草稿' }))
  expect(onPrepare.mock.lastCall?.[0]).toMatchObject({
    inputs: { features: outside }, criteria: { scope, expected_artifact: 'filtered_features' },
  })
})
