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
