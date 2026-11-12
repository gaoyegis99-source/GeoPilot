import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import { ApiError, type CheckReport, type RunChecks, workbenchApi } from '../../api/workbench'
import { ChecksPanel } from './ChecksPanel'

const current: CheckReport = {
  id: 'current-report', run_id: 'road-run', status: 'FAILED',
  rule_versions: { measurement_policy_satisfied: 3 }, created_at: '2026-11-11T12:00:00Z',
  report: {
    completed_rules: ['measurement_policy_satisfied'], pending_rules: [], interruption: null,
    phase_findings: {
      input: [{ rule: 'crs_is_known', phase: 'input', step_key: 'create_buffer',
        field: 'inputs.source.crs', severity: 'blocking', passed: true,
        observed: 'EPSG:32650', expected: 'EPSG:32650', message: '输入坐标参考已核对。' }],
    },
    rules: {
      measurement_policy_satisfied: { passed: false, severity: 'blocking', message: '缓冲参数与确认值不同。',
        observed: { distance: 250, unit: 'meter' }, expected: { distance: 500, unit: 'meter' } },
    },
  },
}

const historical: CheckReport = {
  ...current, id: 'old-report', status: 'WARNING', rule_versions: { measurement_policy_satisfied: 1 },
  report: {
    ...current.report,
    rules: { measurement_policy_satisfied: { passed: false, severity: 'warning',
      message: '历史规则警告。', observed: 1, expected: 2 } },
  },
}

const response: RunChecks = {
  run_id: 'road-run', check_status: 'FAILED', current_report: current, report_ids: [current.id, historical.id],
}

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
})

it('shows method failures alongside passing phase findings and the separate execution state', async () => {
  vi.spyOn(workbenchApi, 'checks').mockResolvedValue(response)
  render(<><p>运行成功</p><ChecksPanel runId="road-run" checkStatus="FAILED" currentReportId={current.id} /></>)

  expect(screen.getByText('运行成功')).toBeTruthy()
  expect(screen.getByRole('status', { name: '当前检查状态' }).textContent).toContain('未通过')
  const methodRule = await screen.findByText('measurement_policy_satisfied')
  const methodDetails = methodRule.closest('details')
  expect(methodDetails?.open).toBe(true)
  expect(methodDetails?.textContent).toContain('缓冲参数与确认值不同。')
  expect(methodDetails?.textContent).toContain('阻断')
  expect(methodDetails?.textContent).toContain('"distance": 250')
  expect(methodDetails?.textContent).toContain('"distance": 500')

  fireEvent.click(screen.getByText('输入检查'))
  const phaseRule = screen.getByText('crs_is_known')
  fireEvent.click(phaseRule.closest('summary')!)
  expect(phaseRule.closest('details')?.textContent).toContain('create_buffer')
  expect(phaseRule.closest('details')?.textContent).toContain('inputs.source.crs')
  expect(phaseRule.closest('details')?.textContent).toContain('EPSG:32650')
})

it('requests an exact historical report and keeps its verdict separate from the current status', async () => {
  const checks = vi.spyOn(workbenchApi, 'checks').mockImplementation(async (_run, reportId) =>
    reportId ? { ...response, selected_report: historical } : response)
  render(<ChecksPanel runId="road-run" checkStatus="FAILED" currentReportId={current.id} />)
  const history = await screen.findByRole('combobox', { name: '检查报告' })
  fireEvent.change(history, { target: { value: historical.id } })
  await screen.findByText('历史规则警告。')
  expect(checks).toHaveBeenLastCalledWith('road-run', historical.id, expect.any(AbortSignal))
  expect(screen.getByRole('status', { name: '当前检查状态' }).textContent).toContain('未通过')
  expect(screen.getByLabelText('报告状态').textContent).toContain('有警告')
  expect(screen.getByText('历史报告')).toBeTruthy()
  const historyRule = screen.getByText('measurement_policy_satisfied').closest('details')
  expect(historyRule?.querySelector('dt')?.nextElementSibling?.textContent).toBe('1')
  fireEvent.change(history, { target: { value: '' } })
  await screen.findByText('缓冲参数与确认值不同。')
  expect(screen.getByLabelText('报告状态').textContent).toContain('未通过')
})

it('reloads when the current report pointer changes and discards obsolete run requests', async () => {
  let completeOld: ((value: RunChecks) => void) | undefined
  const newReport: CheckReport = { ...current, run_id: 'new-run', id: 'new-report', status: 'PASSED',
    report: { ...current.report, rules: { output_exists: {
      passed: true, severity: 'blocking', message: '成果存在。', observed: null,
    } } } }
  const next: RunChecks = { run_id: 'new-run', check_status: 'PASSED',
    current_report: newReport, report_ids: [newReport.id] }
  const checks = vi.spyOn(workbenchApi, 'checks').mockImplementation(async (id) => id === 'road-run'
    ? new Promise<RunChecks>((resolve) => { completeOld = resolve }) : next)

  const view = render(<ChecksPanel runId="road-run" checkStatus="CHECKING" />)
  await waitFor(() => expect(completeOld).toBeDefined())
  view.rerender(<ChecksPanel runId="new-run" checkStatus="PASSED" currentReportId="new-report" />)
  await screen.findByText('output_exists')
  expect(checks.mock.calls[0][2]?.aborted).toBe(true)
  await act(async () => completeOld?.(response))
  expect(screen.queryByText('measurement_policy_satisfied')).toBeNull()
  expect(screen.getByRole('status', { name: '当前检查状态' }).textContent).toContain('通过')
  view.rerender(<ChecksPanel runId="new-run" checkStatus="PASSED" currentReportId="published-later" />)
  await waitFor(() => expect(checks).toHaveBeenCalledTimes(3))
})

it('shows interruption, allows an authorized recheck and keeps retrieval retry separate', async () => {
  const interrupted: CheckReport = {
    ...current, status: 'NOT_CHECKED',
    report: { ...current.report, pending_rules: ['output_exists'], completed_rules: [],
      rules: {}, interruption: { code: 'CHECK_INTERRUPTED', field: 'checks.report', message: '检查未完成。' } },
  }
  const checks = vi.spyOn(workbenchApi, 'checks')
    .mockRejectedValueOnce(new ApiError(500, { code: 'STORAGE_FAILED', message: '报告读取失败。' }))
    .mockResolvedValue({ ...response, check_status: 'NOT_CHECKED', current_report: interrupted })
  const onRecheck = vi.fn()
  const view = render(<ChecksPanel runId="road-run" checkStatus="NOT_CHECKED" onRecheck={onRecheck} />)
  await screen.findByRole('alert')
  fireEvent.click(screen.getByRole('button', { name: '重试读取报告' }))
  await screen.findByText('检查未完成。')
  expect(checks).toHaveBeenCalledTimes(2)
  expect(onRecheck).not.toHaveBeenCalled()
  expect(screen.getByText('output_exists')).toBeTruthy()
  const currentStatus = within(screen.getByRole('status', { name: '当前检查状态' }))
  expect(currentStatus.getByText('未完成')).toBeTruthy()
  fireEvent.click(screen.getByRole('button', { name: '重新检查' }))
  expect(onRecheck).toHaveBeenCalledTimes(1)
  view.rerender(<ChecksPanel runId="road-run" checkStatus="CHECKING" onRecheck={onRecheck} recheckPending />)
  expect((screen.getByRole('button', { name: '正在提交重新检查…' }) as HTMLButtonElement).disabled).toBe(true)
})
