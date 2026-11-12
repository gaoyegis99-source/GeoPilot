import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import { ProblemDetails } from './ProblemDetails'

afterEach(cleanup)

it('keeps the diagnosis, relevant action and log available while details are collapsed', () => {
  const retry = vi.fn()
  render(<ProblemDetails
    problem={{
      code: 'PROCESS_TIMEOUT', phase: 'execute', step_key: 'prepare', field: 'tool',
      message: '处理超时', suggestion: '调整输入后重跑。', observed: { elapsed: 60 },
    }}
    actions={{ retry }}
    logHref="/api/runs/run-1/steps/prepare/log"
  />)
  expect(screen.getByText('处理超时')).toBeTruthy()
  expect(screen.getByRole('link', { name: '查看完整日志' }).getAttribute('href')).toContain('/prepare/log')
  fireEvent.click(screen.getByRole('button', { name: '重新运行' }))
  expect(retry).toHaveBeenCalledExactlyOnceWith()
  const summary = screen.getByText('技术详情')
  expect(summary.closest('details')?.open).toBe(false)
  fireEvent.click(summary)
  expect(summary.closest('details')?.open).toBe(true)
  expect(screen.getByText('PROCESS_TIMEOUT')).toBeTruthy()
  expect(screen.getByText('prepare')).toBeTruthy()
  expect(screen.getByText('tool')).toBeTruthy()
})

it.each([
  ['CHECK_INTERRUPTED', 'check', '重新检查'],
  ['ARTIFACT_CHANGED', 'check', '重新检查'],
  ['METHOD_PRECONDITION_FAILED', 'preflight', '返回草稿'],
  ['PREVIEW_FAILED', 'preview', '重新加载预览'],
])('offers the action that matches %s', (code, phase, name) => {
  render(<ProblemDetails problem={{ code, phase, message: '操作未完成' }} actions={{
    edit: vi.fn(), retry: vi.fn(), recheck: vi.fn(), preview: vi.fn(), register: vi.fn(),
  }} />)
  expect(screen.getByRole('button', { name })).toBeTruthy()
  expect(screen.queryByRole('button', { name: '打开资产登记' })).toBeNull()
})
