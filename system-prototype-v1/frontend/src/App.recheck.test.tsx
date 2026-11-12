import { cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import { App } from './App'
import { type AnalysisPlan, type CheckReport, type RunStatus, workbenchApi } from './api/workbench'

vi.mock('./features/map/MapPanel', () => ({ MapPanel: () => null }))

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
  window.history.replaceState(null, '', '/')
})

it('offers one visible recovery action after cancelling a check and retains it if submission fails', async () => {
  const plan: AnalysisPlan = {
    id: 'plan', version: 1, status: 'confirmed', task_id: 'task', goal: { summary: '核对道路缓冲成果' },
    method: { id: 'road-buffer', version: 1 }, inputs: {}, critical_options: {},
    steps: [], expected_artifacts: [], check_rules: [], issues: [],
  }
  const origin = { run_id: 'run-1', task_id: plan.task_id, plan_id: plan.id, plan_version: plan.version }
  const interruption = {
    code: 'CHECK_CANCELLED', field: 'check_rules.1', message: 'The check was cancelled by request.',
  }
  const status: RunStatus = {
    run_id: origin.run_id, execution_status: 'SUCCEEDED', check_status: 'NOT_CHECKED', current_step: null,
    cancel_requested: false, current_report_id: 'cancelled-report',
    error: { ...interruption, phase: 'check', step_key: 'count', suggestion: 'Check the inputs.', log_path: null },
  }
  const report: CheckReport = {
    id: 'cancelled-report', run_id: origin.run_id, status: 'NOT_CHECKED',
    rule_versions: {}, created_at: '2026-11-11T12:00:00Z',
    report: { completed_rules: [], pending_rules: [], interruption, phase_findings: {}, rules: {} },
  }
  vi.spyOn(workbenchApi, 'assets').mockResolvedValue({ assets: [] })
  vi.spyOn(workbenchApi, 'methods').mockResolvedValue({ methods: [] })
  vi.spyOn(workbenchApi, 'modelAvailability').mockResolvedValue({ available: false })
  vi.spyOn(workbenchApi, 'runOrigin').mockResolvedValue(origin)
  vi.spyOn(workbenchApi, 'task').mockResolvedValue({
    task_id: origin.task_id, goal: plan.goal, context: null, plans: [plan],
    runs: [{ ...origin, execution_status: status.execution_status, check_status: status.check_status }],
  })
  vi.spyOn(workbenchApi, 'plan').mockResolvedValue(plan)
  vi.spyOn(workbenchApi, 'run').mockResolvedValue(status)
  vi.spyOn(workbenchApi, 'steps').mockResolvedValue({ run_id: origin.run_id, steps: [] })
  vi.spyOn(workbenchApi, 'artifacts').mockResolvedValue({
    run_id: origin.run_id, check_status: 'NOT_CHECKED', artifacts: [], diagnostics: [],
  })
  vi.spyOn(workbenchApi, 'checks').mockResolvedValue({
    run_id: origin.run_id, check_status: 'NOT_CHECKED', current_report: report, report_ids: [report.id],
  })
  const recheck = vi.spyOn(workbenchApi, 'recheck')
    .mockRejectedValueOnce(new Error('offline'))
    .mockResolvedValueOnce({ report_id: 'replacement' })
  const start = vi.spyOn(workbenchApi, 'start')

  window.history.replaceState(null, '', '/?run=run-1')
  render(<App />)
  const run = within(screen.getByRole('region', { name: '运行' }))
  const action = await run.findByRole('button', { name: '重新检查' })
  const cancellation = '成果检查已取消，已登记的成果仍然保留。'
  expect(run.getByText('运行成功')).toBeTruthy()
  expect(run.getByText(cancellation)).toBeTruthy()
  expect(run.queryByText('Check the inputs.')).toBeNull()
  const checks = within(screen.getByRole('region', { name: '规则检查' }))
  const summary = checks.getByText('规则检查未完成', { exact: true }).closest('summary')!
  expect(summary.closest('details')?.open).toBe(false)
  fireEvent.click(summary)
  const reportDetails = within(await checks.findByRole('article', { name: '检查报告明细' }))
  expect(reportDetails.getByText(cancellation)).toBeTruthy()
  expect(reportDetails.getByText('CHECK_CANCELLED')).toBeTruthy()
  expect(screen.queryByText(interruption.message)).toBeNull()
  expect(screen.getAllByRole('button', { name: '重新检查' })).toEqual([action])

  fireEvent.click(action)
  expect(await run.findByText('无法提交重新检查。')).toBeTruthy()
  expect(screen.getAllByRole('button', { name: '重新检查' })).toEqual([run.getByRole('button', { name: '重新检查' })])
  fireEvent.click(run.getByRole('button', { name: '重新检查' }))
  expect(await run.findByText('已提交重新检查，等待后端处理。')).toBeTruthy()
  expect(screen.queryByRole('button', { name: '重新检查' })).toBeNull()
  expect(recheck.mock.calls).toEqual([[origin.run_id], [origin.run_id]])
  expect(start).not.toHaveBeenCalled()
})
