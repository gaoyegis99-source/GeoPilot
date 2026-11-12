import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import { App } from '../../App'
import type { PlanComparison } from '../../api/plan-alternatives'
import { type AnalysisPlan, type MethodEntry, workbenchApi } from '../../api/workbench'
import { AlternativePlansPanel } from './AlternativePlansPanel'

vi.mock('../map/MapPanel', () => ({ MapPanel: () => null }))

const plan: AnalysisPlan = {
  id: 'roads-plan', version: 2, task_id: 'roads-task', status: 'confirmed',
  method: { id: 'road-buffer', version: 3 }, goal: { summary: '道路缓冲' },
  inputs: { roads: { asset_id: 'roads', asset_version: 2 } },
  critical_options: { distance_method: 'local_projected', distance_crs_ref: 'EPSG:32650' },
  steps: [{
    key: 'buffer', tool: { id: 'planar-buffer', version: 1 }, inputs: {}, parameters: {}, outputs: {},
  }],
  expected_artifacts: [], check_rules: ['output_exists'], issues: [],
}
const baseline: PlanComparison = {
  plan, criteria: {}, input_types: { roads: 'vector_line' },
  option_specs: {
    distance_method: { type: 'enum', values: ['local_projected', 'geodesic'] },
    distance_crs_ref: { type: 'reference', required_for: 'local_projected' },
  },
  preconditions: ['known_crs'], measurement: { buffer: { distance: 'local_projected' } },
}
const geographic: PlanComparison = {
  ...baseline,
  plan: {
    ...plan, status: 'draft', critical_options: { distance_method: 'geodesic' },
    steps: [{ ...plan.steps[0], tool: { id: 'geodesic-buffer', version: 1 } }],
  },
  measurement: { buffer: { distance: 'geodesic' } },
}

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
  window.history.replaceState(null, '', '/')
})

it('compares inputs, parameters and tools, then waits for explicit draft selection', async () => {
  const load = vi.fn(async () => ({ baseline, alternatives: [geographic] }))
  const select = vi.fn()
  const page = render(<AlternativePlansPanel plan={plan} disabled={false} load={load} onSelect={select} />)
  expect(load).not.toHaveBeenCalled()
  const disclosure = page.container.querySelector('details')
  if (!disclosure) throw new Error('Expected the comparison disclosure')
  disclosure.open = true
  fireEvent(disclosure, new Event('toggle'))
  await waitFor(() => expect(load).toHaveBeenCalledWith(plan.id, plan.version, expect.any(AbortSignal)))
  expect(await screen.findByRole('table', { name: '输入角色和资产版本' })).toBeTruthy()
  expect(screen.getByRole('table', { name: '关键参数' })).toBeTruthy()
  expect(screen.getByText('geodesic-buffer v1')).toBeTruthy()
  expect(select).not.toHaveBeenCalled()
  fireEvent.click(screen.getByRole('button', { name: '采用并生成草稿' }))
  expect(select).toHaveBeenCalledOnce()
  expect(select).toHaveBeenCalledWith(geographic)
})

it('saves the selected alternative as a new draft of the original plan', async () => {
  const next: AnalysisPlan = { ...geographic.plan, version: 3 }
  const method: MethodEntry = {
    title: '道路缓冲', description: '根据道路距离生成缓冲区', available: true, issues: [],
    definition: {
      ...plan.method, locked_order: true, inputs: { roads: 'vector_line' },
      critical_options: {
        distance_method: { type: 'enum', values: ['local_projected', 'geodesic'] },
        distance_crs_ref: { type: 'reference', required_for: 'local_projected' },
      },
      steps: [], check_rules: [],
    },
  }
  const record = { task_id: plan.task_id, goal: plan.goal, context: null, plans: [plan], runs: [] }
  vi.spyOn(workbenchApi, 'assets').mockResolvedValue({ assets: [] })
  vi.spyOn(workbenchApi, 'methods').mockResolvedValue({ methods: [method] })
  vi.spyOn(workbenchApi, 'modelAvailability').mockResolvedValue({ available: false })
  vi.spyOn(workbenchApi, 'task').mockResolvedValueOnce(record).mockResolvedValue({ ...record, plans: [next, plan] })
  vi.spyOn(workbenchApi, 'plan').mockResolvedValue(plan)
  vi.spyOn(workbenchApi, 'previewContext').mockResolvedValue({ task_id: 'preview', goal: plan.goal, method: plan.method, roles: {} })
  vi.spyOn(workbenchApi, 'alternatives').mockResolvedValue({ baseline, alternatives: [geographic] })
  const save = vi.spyOn(workbenchApi, 'draft').mockResolvedValue(next)
  const confirm = vi.spyOn(workbenchApi, 'confirm')
  const run = vi.spyOn(workbenchApi, 'start')
  window.history.replaceState(null, '', '/?task=roads-task&plan=roads-plan&version=2')

  render(<App />)
  const summary = await screen.findByText('比较替代方案')
  const disclosure = summary.closest('details')
  if (!disclosure) throw new Error('Expected the workbench comparison disclosure')
  disclosure.open = true
  fireEvent(disclosure, new Event('toggle'))
  expect(save).not.toHaveBeenCalled()
  fireEvent.click(await screen.findByRole('button', { name: '采用并生成草稿' }))
  await waitFor(() => expect(window.location.search).toContain('version=3'))
  expect(save).toHaveBeenCalledWith(plan.task_id, {
    base_plan_id: plan.id, base_plan_version: 2, goal: plan.goal, method: next.method,
    inputs: next.inputs, critical_options: next.critical_options, criteria: geographic.criteria,
  })
  await waitFor(() => expect((screen.getByRole('button', { name: '确认方案' }) as HTMLButtonElement).disabled).toBe(false))
  expect((screen.getByRole('button', { name: '开始运行' }) as HTMLButtonElement).disabled).toBe(true)
  expect(confirm).not.toHaveBeenCalled()
  expect(run).not.toHaveBeenCalled()
})
