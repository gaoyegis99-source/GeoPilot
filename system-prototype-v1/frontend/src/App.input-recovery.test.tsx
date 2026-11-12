import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import { App } from './App'
import { ApiError, type AnalysisPlan, type AssetEntry, type MethodEntry, workbenchApi } from './api/workbench'

vi.mock('./features/map/MapPanel', () => ({ MapPanel: () => null }))

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
  window.history.replaceState(null, '', '/')
})

it('opens registration after changed input and requires explicit selection to revise the saved plan', async () => {
  const path = '/gis/sample.gpkg'
  const oldRef = { asset_id: 'road-source', asset_version: 1 }
  const newRef = { ...oldRef, asset_version: 2 }
  const original: AssetEntry = {
    ...oldRef, source: { path, layer: 'roads' }, source_type: 'local_file', availability: 'AVAILABLE',
    created_at: '2026-11-11T12:00:00Z',
    metadata: { kind: 'vector', geometry_type: 'LineString', format: 'GPKG' },
  }
  const method: MethodEntry = {
    title: '道路缓冲统计', description: '', available: true, issues: [],
    definition: { id: 'road-buffer', version: 3, locked_order: true, inputs: { roads: 'vector_line' },
      critical_options: {}, steps: [], check_rules: [] },
  }
  const plan: AnalysisPlan = {
    id: 'plan', version: 1, status: 'confirmed', task_id: 'task', goal: { summary: '统计道路缓冲范围' },
    method: { id: method.definition.id, version: method.definition.version }, inputs: { roads: oldRef },
    critical_options: {}, steps: [], expected_artifacts: [], check_rules: [], issues: [],
  }
  const revision: AnalysisPlan = { ...plan, version: 2, status: 'draft', inputs: { roads: newRef } }
  const origin = { run_id: 'old-run', task_id: plan.task_id, plan_id: plan.id, plan_version: plan.version }
  const priorStatus = { execution_status: 'SUCCEEDED', check_status: 'PASSED' } as const
  const task = {
    task_id: origin.task_id, goal: plan.goal, context: null, plans: [plan], runs: [{ ...origin, ...priorStatus }],
  }
  const originalMessage = `Source content differs from asset 'road-source' version 1: ${path}`
  vi.spyOn(workbenchApi, 'assets').mockResolvedValue({ assets: [original] })
  vi.spyOn(workbenchApi, 'methods').mockResolvedValue({ methods: [method] })
  vi.spyOn(workbenchApi, 'modelAvailability').mockResolvedValue({ available: false })
  vi.spyOn(workbenchApi, 'runOrigin').mockResolvedValue(origin)
  vi.spyOn(workbenchApi, 'task').mockResolvedValueOnce(task).mockResolvedValue({ ...task, plans: [revision, plan] })
  vi.spyOn(workbenchApi, 'plan').mockResolvedValue(plan)
  vi.spyOn(workbenchApi, 'previewContext').mockResolvedValue({
    task_id: 'preview', goal: plan.goal, method: plan.method, roles: {},
  })
  vi.spyOn(workbenchApi, 'run').mockResolvedValue({
    run_id: origin.run_id, ...priorStatus, current_step: null, cancel_requested: false, current_report_id: null, error: null,
  })
  vi.spyOn(workbenchApi, 'steps').mockResolvedValue({ run_id: origin.run_id, steps: [] })
  vi.spyOn(workbenchApi, 'artifacts').mockResolvedValue({
    run_id: origin.run_id, check_status: 'PASSED', artifacts: [], diagnostics: [],
  })
  vi.spyOn(workbenchApi, 'checks').mockResolvedValue({
    run_id: origin.run_id, check_status: 'PASSED', current_report: null, report_ids: [],
  })
  vi.spyOn(workbenchApi, 'layers').mockResolvedValue({ layers: [{ name: 'roads', geometry_type: 'LineString' }] })
  const start = vi.spyOn(workbenchApi, 'start').mockRejectedValue(new ApiError(409, {
    code: 'INPUT_CHANGED', field: 'source.path', message: originalMessage,
  }))
  const register = vi.spyOn(workbenchApi, 'register').mockResolvedValue({ ...original, ...newRef })
  const draft = vi.spyOn(workbenchApi, 'draft').mockResolvedValue(revision)

  window.history.replaceState(null, '', '/?run=old-run')
  render(<App />)
  const run = within(screen.getByRole('region', { name: '运行' }))
  await run.findByText('运行成功')
  fireEvent.click(run.getByRole('button', { name: '开始运行' }))
  const action = await run.findByRole('button', { name: '打开资产登记' })
  const alert = within(run.getByRole('alert'))
  expect(alert.getByText('输入数据')).toBeTruthy()
  expect(alert.getByText('本机输入文件与方案中的登记版本不一致。')).toBeTruthy()
  expect(alert.getByText(/重新登记.*选择新版本.*生成并确认草稿/)).toBeTruthy()
  expect(run.getByText('INPUT_CHANGED')).toBeTruthy()
  expect(run.getByText(originalMessage)).toBeTruthy()
  expect(start.mock.calls).toEqual([[plan.id, plan.version]])

  fireEvent.click(action)
  const assets = within(screen.getByRole('region', { name: '数据' }))
  const source = await assets.findByRole('textbox', { name: '本机文件路径' })
  expect(document.activeElement).toBe(source)
  expect(register).not.toHaveBeenCalled()
  fireEvent.change(source, { target: { value: path } })
  fireEvent.click(assets.getByRole('button', { name: '读取图层' }))
  const layer = await assets.findByRole('combobox', { name: 'GeoPackage 图层' })
  fireEvent.change(layer, { target: { value: 'roads' } })
  fireEvent.click(assets.getByRole('button', { name: '登记资产' }))
  await assets.findByRole('checkbox', { name: '显示 roads v2' })
  expect(register.mock.calls).toEqual([[path, 'roads']])

  const form = within(screen.getByRole('form', { name: '方法表单' }))
  const input = form.getByRole<HTMLSelectElement>('combobox', { name: '道路' })
  expect(input.value).toBe('road-source@1')
  expect(draft).not.toHaveBeenCalled()
  fireEvent.change(input, { target: { value: 'road-source@2' } })
  expect(window.location.search).toContain('version=1')
  fireEvent.click(form.getByRole('button', { name: '生成草稿' }))
  await waitFor(() => expect(window.location.search).toContain('version=2'))
  expect(draft).toHaveBeenCalledExactlyOnceWith(plan.task_id, expect.objectContaining({
    base_plan_id: plan.id, base_plan_version: plan.version, inputs: { roads: newRef },
  }))
  expect(window.location.search).toContain('run=old-run')
  expect(run.getByText('运行成功')).toBeTruthy()
  expect(start).toHaveBeenCalledOnce()
})
