import { cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import type { AnalysisPlan, AssetEntry } from '../../../api/workbench'
import { PlanPreview } from './PlanPreview'

afterEach(cleanup)

const plan: AnalysisPlan = {
  id: 'plan-road', version: 3, status: 'draft', task_id: 'task-road', goal: { summary: '统计道路附近的建筑' },
  method: { id: 'road-buffer-statistics', version: 2 },
  inputs: { roads: { asset_id: 'roads-id', asset_version: 1 }, zones: { asset_id: 'zones-id', asset_version: 4 } },
  critical_options: {
    distance_m: { value: 0.5, unit: 'kilometer' }, distance_crs_ref: 'EPSG:32650', count: 0,
    dissolve: false, mapping: { keys: ['A', 'B'], unmatched: null }, empty: '',
  },
  steps: [
    { key: 'z_buffer', tool: { id: 'qgis-buffer', version: 2 }, inputs: { source: { asset: 'roads' } },
      parameters: { distance: { value: 0.5, unit: 'kilometer' }, segments: 16, dissolve: false },
      outputs: { buffer: { type: 'vector_polygon', target: 'intermediate/z_buffer/buffer.gpkg', layer: 'buffer' } } },
    { key: 'a_count', tool: { id: 'python-count', version: 1 }, inputs: { source: { step: 'z_buffer', output: 'buffer' } },
      parameters: { mode: 'count' }, outputs: {} },
  ],
  expected_artifacts: [{ role: 'influence', step: 'z_buffer', output: 'buffer', type: 'vector_polygon' }],
  check_rules: ['output_exists', 'measurement_policy_satisfied'], issues: [],
}

function roadAsset(version: number, layer: string): AssetEntry {
  return {
    asset_id: 'roads-id', asset_version: version, source: { path: '/tmp/roads.gpkg', layer },
    source_type: 'local_file', availability: 'AVAILABLE', created_at: '2026-11-11T00:00:00Z',
    metadata: { kind: 'vector', format: 'GPKG', geometry_type: 'LineString' },
  }
}

it('shows the exact plan and asset versions with all declared option values', () => {
  render(<PlanPreview plan={plan} assets={[roadAsset(2, 'new-road-layer'), roadAsset(1, 'actual-roads')]} />)

  const preview = screen.getByRole('region', { name: '方案预览' })
  expect(within(preview).getByText('统计道路附近的建筑')).toBeTruthy()
  expect(within(preview).getByText('plan-road')).toBeTruthy()
  expect(within(preview).getByText('版本 3')).toBeTruthy()
  expect(within(preview).getByText('road-buffer-statistics')).toBeTruthy()
  const inputs = within(preview).getByRole('region', { name: '输入角色' })
  expect(within(inputs).getByText('actual-roads')).toBeTruthy()
  expect(within(inputs).queryByText('new-road-layer')).toBeNull()
  expect(within(inputs).getByText('zones-id')).toBeTruthy()
  expect(within(inputs).getByText('版本 4')).toBeTruthy()

  const options = within(preview).getByRole('region', { name: '关键选项' })
  const rows = within(options).getAllByRole('term')
  expect(rows.map(row => row.textContent)).toEqual(Object.keys(plan.critical_options))
  for (const [key, value] of Object.entries(plan.critical_options)) {
    const rendered = within(options).getByText(key, { exact: true }).closest('dt')!.nextElementSibling!
    expect(JSON.parse(rendered.textContent!)).toEqual(value)
  }
})

it('preserves server step order and expands precise tools, parameters and output bindings', () => {
  render(<PlanPreview plan={{ ...plan, status: 'confirmed' }} />)
  expect(screen.getByText('已确认')).toBeTruthy()
  const steps = screen.getByRole('list', { name: '方案步骤' })
  const [first, second] = within(steps).getAllByRole('listitem')
  expect(within(first).getByText('z_buffer')).toBeTruthy()
  expect(within(second).getByText('a_count')).toBeTruthy()
  const summary = within(first).getByText('z_buffer').closest('summary')!
  expect((summary.parentElement as HTMLDetailsElement).open).toBe(false)
  expect(within(first).getByText('qgis-buffer')).toBeTruthy()
  expect(within(first).getByText('版本 2')).toBeTruthy()
  fireEvent.click(summary)
  expect((summary.parentElement as HTMLDetailsElement).open).toBe(true)
  const distance = within(first).getByText('distance', { exact: true }).closest('dt')!.nextElementSibling!
  expect(JSON.parse(distance.textContent!)).toEqual({ value: 0.5, unit: 'kilometer' })
  expect(within(first).getByText('intermediate/z_buffer/buffer.gpkg')).toBeTruthy()
})

it('shows issues with or without field paths and emits only an exact field on navigation', () => {
  const onIssueSelect = vi.fn()
  const issues = [
    { code: 'INPUT_REQUIRED', field: 'inputs.roads', message: '请选择道路资产' },
    { code: 'METHOD_NOT_READY', field: null, message: '方案定义尚未就绪' },
  ]
  const incomplete = { ...plan, inputs: {}, critical_options: {}, steps: [{ ...plan.steps[0], tool: null }], issues }
  const { rerender } = render(<PlanPreview plan={incomplete} onIssueSelect={onIssueSelect} />)

  const pending = screen.getByRole('region', { name: '待补项' })
  expect(within(pending).getByText('请选择道路资产')).toBeTruthy()
  expect(within(pending).getByText('方案定义尚未就绪')).toBeTruthy()
  const buttons = within(pending).getAllByRole('button')
  expect(buttons).toHaveLength(1)
  fireEvent.click(within(pending).getByRole('button', { name: '定位 inputs.roads' }))
  expect(onIssueSelect).toHaveBeenCalledExactlyOnceWith('inputs.roads')
  expect(screen.getByText('待绑定工具')).toBeTruthy()

  rerender(<PlanPreview plan={incomplete} />)
  expect(within(pending).getByText('inputs.roads')).toBeTruthy()
  expect(within(pending).queryByRole('button')).toBeNull()
})
