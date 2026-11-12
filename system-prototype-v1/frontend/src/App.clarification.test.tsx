import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import { App } from './App'
import { type AnalysisPlan, type AssetEntry, type ClarificationReport, type MethodEntry, workbenchApi } from './api/workbench'

vi.mock('./features/map/MapPanel', () => ({ MapPanel: () => null }))

const asset: AssetEntry = {
  asset_id: 'roads', asset_version: 2, source: { path: '/roads.gpkg', layer: 'roads' },
  source_type: 'local_file', availability: 'AVAILABLE', created_at: '2026-11-12T00:00:00Z',
  metadata: { kind: 'vector', format: 'GPKG', geometry_type: 'LineString' },
}
const ref = { asset_id: asset.asset_id, asset_version: asset.asset_version }
const method: MethodEntry = {
  title: '道路缓冲', description: '根据距离生成缓冲区', available: true, issues: [],
  definition: {
    id: 'road-buffer', version: 1, locked_order: true, inputs: { roads: 'vector_line' },
    critical_options: { distance_m: { type: 'number', minimum: 1 } },
    steps: [], check_rules: [],
  },
}
const first: AnalysisPlan = {
  id: 'plan', version: 1, status: 'draft', task_id: 'task', goal: { summary: '为道路生成缓冲区' },
  method: { id: method.definition.id, version: method.definition.version },
  inputs: {}, critical_options: {}, steps: [], expected_artifacts: [], check_rules: [],
  issues: [
    { code: 'INPUT_REQUIRED', field: 'inputs.roads', message: '请选择道路数据' },
    { code: 'OPTION_REQUIRED', field: 'critical_options.distance_m', message: '请填写缓冲距离' },
  ],
}
const second: AnalysisPlan = { ...first, version: 2, inputs: { roads: ref }, issues: first.issues.slice(1) }
const third: AnalysisPlan = { ...second, version: 3, critical_options: { distance_m: 250 }, issues: [] }

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
  window.history.replaceState(null, '', '/')
})

it('answers one field per version, restores the current draft, and keeps explicit confirmation', async () => {
  let latest = first
  const plans = [third, second, first]
  vi.spyOn(workbenchApi, 'assets').mockResolvedValue({ assets: [asset] })
  vi.spyOn(workbenchApi, 'methods').mockResolvedValue({ methods: [method] })
  vi.spyOn(workbenchApi, 'modelAvailability').mockResolvedValue({ available: false })
  vi.spyOn(workbenchApi, 'task').mockImplementation(async () => ({
    task_id: latest.task_id, goal: latest.goal, context: null, runs: [],
    plans: plans.filter(plan => plan.version <= latest.version),
  }))
  const loadPlan = vi.spyOn(workbenchApi, 'plan').mockImplementation(async (_, version) => {
    const plan = plans.find(entry => entry.version === version)
    if (!plan) throw new Error('Unexpected plan version')
    return plan
  })
  vi.spyOn(workbenchApi, 'previewContext').mockResolvedValue({
    task_id: 'preview', goal: first.goal, method: first.method, roles: {},
  })
  vi.spyOn(workbenchApi, 'clarification').mockImplementation(async (_, version) => ({
    schema_version: 1, task_id: latest.task_id, plan_id: latest.id, plan_version: version,
    method: latest.method, status: 'blocked',
    items: latest.issues.map(issue => ({
      kind: issue.field === 'inputs.roads' ? 'input' : 'option', field: issue.field ?? '',
      code: issue.code, reason: issue.message, options: issue.field === 'inputs.roads' ? [ref] : [],
    })),
  } satisfies ClarificationReport))
  const answer = vi.spyOn(workbenchApi, 'answerClarification').mockImplementation(async (_, version) => {
    latest = version === 1 ? second : third
    return latest
  })
  const confirm = vi.spyOn(workbenchApi, 'confirm')
  const start = vi.spyOn(workbenchApi, 'start')

  window.history.replaceState(null, '', '/?task=task&plan=plan&version=1')
  const current = render(<App />)
  let question = within(await screen.findByRole('form', { name: '澄清回答' }))
  const roads = question.getByLabelText('道路')
  const versionTwo = within(roads).getByRole('option', { name: 'roads.gpkg · roads · v2' }) as HTMLOptionElement
  fireEvent.change(roads, { target: { value: versionTwo.value } })
  fireEvent.click(question.getByRole('button', { name: '保存此回答' }))
  await waitFor(() => expect(window.location.search).toContain('version=2'))
  expect(answer).toHaveBeenNthCalledWith(1, 'plan', 1, 'inputs.roads', ref)

  current.unmount()
  render(<App />)
  question = within(await screen.findByRole('form', { name: '澄清回答' }))
  expect(loadPlan).toHaveBeenLastCalledWith('plan', 2, expect.any(AbortSignal))
  expect((screen.getByRole('button', { name: '确认方案' }) as HTMLButtonElement).disabled).toBe(true)
  fireEvent.change(question.getByLabelText('缓冲距离'), { target: { value: '250' } })
  fireEvent.click(question.getByRole('button', { name: '保存此回答' }))
  await waitFor(() => expect(window.location.search).toContain('version=3'))
  expect(answer).toHaveBeenNthCalledWith(2, 'plan', 2, 'critical_options.distance_m', 250)
  await waitFor(() => expect((screen.getByRole('button', { name: '确认方案' }) as HTMLButtonElement).disabled).toBe(false))
  expect((screen.getByRole('button', { name: '开始运行' }) as HTMLButtonElement).disabled).toBe(true)
  expect(confirm).not.toHaveBeenCalled()
  expect(start).not.toHaveBeenCalled()
})
