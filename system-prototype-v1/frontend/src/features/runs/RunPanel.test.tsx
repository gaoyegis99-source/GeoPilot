import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import { type RunStatus, workbenchApi } from '../../api/workbench'
import { RunPanel } from './RunPanel'

const running: RunStatus = {
  run_id: 'run-1', execution_status: 'RUNNING', check_status: 'NOT_CHECKED',
  current_step: 'prepare', cancel_requested: false, current_report_id: null, error: null,
}
const step = {
  step_key: 'prepare', position: 0, status: 'RUNNING' as const, log_available: true,
  started_at: '2026-11-11T00:00:00Z', finished_at: null, error: null,
}

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
  vi.useRealTimers()
})

it('polls actual statuses until both phases finish and allows a retained log to open', async () => {
  vi.useFakeTimers()
  const finished = { ...running, execution_status: 'SUCCEEDED', check_status: 'PASSED' } as const
  const poll = vi.spyOn(workbenchApi, 'run').mockResolvedValueOnce(running).mockResolvedValueOnce(finished)
  vi.spyOn(workbenchApi, 'steps').mockResolvedValue({ run_id: 'run-1', steps: [step] })
  vi.spyOn(workbenchApi, 'log').mockResolvedValue('Actual GIS output')
  const onStatus = vi.fn()
  render(<RunPanel runId="run-1" onStatus={onStatus} pollMilliseconds={20} />)
  await act(async () => { await vi.advanceTimersByTimeAsync(0) })
  expect(screen.getAllByText('prepare').length).toBeGreaterThan(0)
  await act(async () => { await vi.advanceTimersByTimeAsync(20) })
  expect(onStatus).toHaveBeenLastCalledWith(finished)
  await act(async () => { await vi.advanceTimersByTimeAsync(120) })
  expect(poll).toHaveBeenCalledTimes(2)
  await act(async () => { fireEvent.click(screen.getByRole('button', { name: '查看日志' })) })
  expect(screen.getByText('Actual GIS output')).toBeTruthy()
  expect(screen.getByRole('link', { name: '查看完整日志' }).getAttribute('href')).toContain('/prepare/log')
})

it('shows a server cancellation request and waits for the terminal server state', async () => {
  vi.useFakeTimers()
  const cancelled = { ...running, execution_status: 'CANCELLED', cancel_requested: true } as const
  vi.spyOn(workbenchApi, 'run').mockResolvedValueOnce(running).mockResolvedValueOnce(cancelled)
  vi.spyOn(workbenchApi, 'steps').mockResolvedValue({ run_id: 'run-1', steps: [step] })
  const cancel = vi.spyOn(workbenchApi, 'cancel').mockResolvedValue({ ...running, cancel_requested: true })
  const onStatus = vi.fn()
  render(<RunPanel runId="run-1" onStatus={onStatus} pollMilliseconds={20} />)
  await act(async () => { await vi.advanceTimersByTimeAsync(0) })
  await act(async () => { fireEvent.click(screen.getByRole('button', { name: '取消运行' })) })
  expect(cancel).toHaveBeenCalledWith('run-1')
  expect(screen.getByText('已请求取消')).toBeTruthy()
  expect(onStatus).toHaveBeenLastCalledWith({ ...running, cancel_requested: true })
  await act(async () => { await vi.advanceTimersByTimeAsync(20) })
  expect(onStatus).toHaveBeenLastCalledWith(cancelled)
  expect(screen.queryByRole('button', { name: '取消运行' })).toBeNull()
})

it('waits for a queued recheck to replace the previous report before stopping polling', async () => {
  vi.useFakeTimers()
  const previous = { ...running, execution_status: 'SUCCEEDED', current_report_id: 'previous' } as const
  const checking = { ...previous, check_status: 'CHECKING' } as const
  const finished = { ...previous, check_status: 'PASSED', current_report_id: 'replacement' } as const
  const poll = vi.spyOn(workbenchApi, 'run')
    .mockResolvedValueOnce(previous).mockResolvedValueOnce(checking).mockResolvedValueOnce(finished)
  vi.spyOn(workbenchApi, 'steps').mockResolvedValue({ run_id: 'run-1', steps: [step] })
  const onStatus = vi.fn()
  render(<RunPanel runId="run-1" pendingReportId="replacement" onStatus={onStatus} pollMilliseconds={20} />)
  await act(async () => { await vi.advanceTimersByTimeAsync(40) })
  expect(onStatus).toHaveBeenNthCalledWith(2, checking)
  expect(onStatus).toHaveBeenLastCalledWith(finished)
  await act(async () => { await vi.advanceTimersByTimeAsync(120) })
  expect(poll).toHaveBeenCalledTimes(3)
})

it('loads on demand and labels evidence separately from a possible cause', async () => {
  vi.spyOn(workbenchApi, 'run').mockResolvedValue({ ...running, execution_status: 'FAILED', check_status: 'FAILED' })
  vi.spyOn(workbenchApi, 'steps').mockResolvedValue({ run_id: 'run-1', steps: [] })
  const feedback = vi.spyOn(workbenchApi, 'feedback').mockResolvedValue({
    run_id: 'run-1',
    steps: [{ step_key: 'prepare', exit_code: -15 }],
    findings: [{ index: 0, rule: 'area_sanity', severity: 'blocking' }],
    statements: [
      { kind: 'fact', text: 'Time limit reached.', step_key: 'prepare', error_code: 'PROCESS_TIMEOUT', check_index: null },
      { kind: 'inference', text: 'Large inputs may have contributed.', step_key: null, error_code: null, check_index: null },
      { kind: 'fact', text: 'Check failed.', step_key: null, error_code: null, check_index: 0 },
    ],
  })
  render(<RunPanel runId="run-1" />)
  const button = await screen.findByRole('button', { name: '查看执行解释' })
  expect(feedback).not.toHaveBeenCalled()
  fireEvent.click(button)
  expect(await screen.findByText('Time limit reached.')).toBeTruthy()
  expect(screen.getAllByText('记录事实')).toHaveLength(2)
  expect(screen.getByText('可能原因')).toBeTruthy()
  expect(screen.getByText('PROCESS_TIMEOUT')).toBeTruthy()
  expect(screen.getByText('area_sanity')).toBeTruthy()
  expect(feedback).toHaveBeenCalledWith('run-1', expect.any(AbortSignal))
})

it('labels cancellation of checks and stops calling a completed calculation the current step', async () => {
  const checking = { ...running, execution_status: 'SUCCEEDED', check_status: 'CHECKING' } as const
  vi.spyOn(workbenchApi, 'run').mockResolvedValue(checking)
  vi.spyOn(workbenchApi, 'steps').mockResolvedValue({
    run_id: 'run-1', steps: [{ ...step, status: 'SUCCEEDED', finished_at: '2026-11-11T00:00:04Z' }],
  })
  const cancel = vi.spyOn(workbenchApi, 'cancel').mockResolvedValue({ ...checking, cancel_requested: true })
  render(<RunPanel runId="run-1" />)

  expect(await screen.findByText('正在检查成果…')).toBeTruthy()
  expect(await screen.findByText(/最后完成的计算步骤/)).toBeTruthy()
  expect(screen.queryByText(/当前步骤/)).toBeNull()
  expect(screen.queryByRole('button', { name: '取消运行' })).toBeNull()
  await act(async () => { fireEvent.click(screen.getByRole('button', { name: '取消检查' })) })
  expect(cancel).toHaveBeenCalledWith('run-1')
  expect(screen.getByRole('button', { name: '已请求取消检查' })).toHaveProperty('disabled', true)
})

it('offers the original plan and its logs when a worker was cancelled in preflight', async () => {
  vi.spyOn(workbenchApi, 'run').mockResolvedValue({
    ...running, execution_status: 'CANCELLED', cancel_requested: true,
    error: {
      phase: 'preflight', code: 'PROCESS_CANCELLED', step_key: 'prepare', field: 'tool',
      message: 'Cancelled.', suggestion: 'Check the inputs.', log_path: 'steps/prepare/process.log',
    },
  })
  vi.spyOn(workbenchApi, 'steps').mockResolvedValue({ run_id: 'run-1', steps: [] })
  const retry = vi.fn()
  render(<RunPanel runId="run-1" actions={{ edit: vi.fn(), retry }} />)

  const rerun = await screen.findByRole('button', { name: '按原方案重新运行' })
  expect(screen.queryByRole('button', { name: '返回草稿' })).toBeNull()
  expect(screen.getByRole('link', { name: '查看完整日志' }).getAttribute('href')).toContain('/prepare/log')
  fireEvent.click(rerun)
  expect(retry).toHaveBeenCalledOnce()
})
