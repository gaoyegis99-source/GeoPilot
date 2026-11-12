import { cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import type { ModelPreview } from '../../api/workbench'
import { ModelAssistPanel, type ModelAssistPanelProps } from './ModelAssistPanel'

afterEach(cleanup)

const goal = '统计道路附近的建筑'
const suggestion: ModelPreview = {
  method: { id: 'road-buffer-statistics', version: 2 }, goal: { summary: goal },
  options: { distance_m: { value: 0.5, unit: 'kilometer' }, keep_empty: false, tolerance: 0 },
  missing: ['distance_crs_ref'], explanation: '先计算道路影响范围，再统计建筑。', explanation_issue: null,
  issues: [{ code: 'INPUT_REQUIRED', field: 'inputs.targets', message: '请选择建筑物点' }],
}

function properties(updates: Partial<ModelAssistPanelProps> = {}) {
  return {
    available: true, pending: false, goal, showGoal: true, suggestion: null, problem: null,
    onGoal: vi.fn(), onRequest: vi.fn(), onApply: vi.fn(), onManual: vi.fn(),
    ...updates,
  } satisfies ModelAssistPanelProps
}

it('retains the existing goal and a manual entry when model assistance is unconfigured', () => {
  const props = properties({ available: false })
  const { rerender } = render(<ModelAssistPanel {...props} />)

  expect(screen.getByText('模型辅助未配置')).toBeTruthy()
  expect(screen.getByLabelText<HTMLTextAreaElement>('任务目标').value).toBe(goal)
  expect(screen.getByRole<HTMLButtonElement>('button', { name: '获取建议' }).disabled).toBe(true)
  fireEvent.click(screen.getByRole('button', { name: '手工填写' }))
  expect(props.onManual).toHaveBeenCalledTimes(1)
  expect(props.onGoal).not.toHaveBeenCalled()
  expect(props.onRequest).not.toHaveBeenCalled()
  expect(props.onApply).not.toHaveBeenCalled()

  rerender(<ModelAssistPanel {...props} available={null} />)
  expect(screen.getByRole('status').textContent).toBe('正在检查模型辅助…')
  expect(screen.getByRole<HTMLButtonElement>('button', { name: '手工填写' }).disabled).toBe(false)
})

it('uses the shared controlled goal without submitting or duplicating the method form', () => {
  const props = properties({ goal: ' ' })
  const { rerender } = render(<ModelAssistPanel {...props} />)
  expect(screen.getByRole<HTMLButtonElement>('button', { name: '获取建议' }).disabled).toBe(true)
  fireEvent.change(screen.getByLabelText('任务目标'), { target: { value: goal } })
  expect(props.onGoal).toHaveBeenCalledExactlyOnceWith(goal)
  expect(props.onRequest).not.toHaveBeenCalled()

  rerender(<ModelAssistPanel {...props} goal={goal} showGoal={false} />)
  expect(screen.queryByLabelText('任务目标')).toBeNull()
  fireEvent.click(screen.getByRole('button', { name: '获取建议' }))
  expect(props.onRequest).toHaveBeenCalledTimes(1)
  expect(props.onApply).not.toHaveBeenCalled()
})

it('keeps manual entry usable while a request is pending and avoids form submission', () => {
  const props = properties({ pending: true, suggestion })
  const onSubmit = vi.fn()
  render(<form onSubmit={onSubmit}><ModelAssistPanel {...props} /></form>)
  expect(screen.getByRole<HTMLButtonElement>('button', { name: '正在获取建议…' }).disabled).toBe(true)
  expect(screen.getByRole<HTMLButtonElement>('button', { name: '应用建议' }).disabled).toBe(true)
  fireEvent.click(screen.getByRole('button', { name: '手工填写' }))
  expect(props.onManual).toHaveBeenCalledTimes(1)
  expect(onSubmit).not.toHaveBeenCalled()
})

it('shows exact method version, safe explanation, checked values and gaps before explicit application', () => {
  const explanation = '<b>实际模型说明</b>'
  const props = properties({ suggestion: { ...suggestion, explanation } })
  const onSubmit = vi.fn()
  const { container } = render(<form onSubmit={onSubmit}><ModelAssistPanel {...props} /></form>)
  const preview = screen.getByRole('region', { name: '建议预览' })
  expect(within(preview).getByText('road-buffer-statistics')).toBeTruthy()
  expect(within(preview).getByText('版本 2')).toBeTruthy()
  expect(within(preview).getByText('distance_crs_ref')).toBeTruthy()
  expect(within(preview).getByText('请选择建筑物点')).toBeTruthy()
  expect(within(preview).getByText('inputs.targets')).toBeTruthy()
  expect(within(preview).getByText(explanation)).toBeTruthy()
  expect(container.querySelector('b')).toBeNull()
  for (const [name, value] of Object.entries(suggestion.options)) {
    const field = within(preview).getByText(name, { exact: true }).closest('dt')!.nextElementSibling!
    expect(JSON.parse(field.textContent!)).toEqual(value)
  }
  expect(props.onApply).not.toHaveBeenCalled()
  fireEvent.click(within(preview).getByRole('button', { name: '应用建议' }))
  expect(props.onApply).toHaveBeenCalledTimes(1)
  expect(props.onRequest).not.toHaveBeenCalled()
  expect(props.onGoal).not.toHaveBeenCalled()
  expect(onSubmit).not.toHaveBeenCalled()
})

it('retains manual fallback after a model error and keeps valid suggestions when explanation fails', () => {
  const props = properties({
    problem: { code: 'MODEL_TIMEOUT', message: '模型请求超时' },
    suggestion: { ...suggestion, explanation: null, explanation_issue: {
      code: 'EXPLANATION_INVALID', field: 'explanation', message: '说明暂不可用',
    } },
  })
  render(<ModelAssistPanel {...props} />)

  expect(screen.getByRole('alert').textContent).toContain('模型请求超时')
  expect(screen.getByText('说明暂不可用')).toBeTruthy()
  fireEvent.click(screen.getByRole('button', { name: '手工填写' }))
  expect(props.onManual).toHaveBeenCalledTimes(1)
  expect(props.onGoal).not.toHaveBeenCalled()
  fireEvent.click(screen.getByRole('button', { name: '应用建议' }))
  expect(props.onApply).toHaveBeenCalledTimes(1)
})

it('shows a distinct proposed goal while preserving the current goal input on application', () => {
  const props = properties({ suggestion })
  const { rerender } = render(<ModelAssistPanel {...props} />)
  expect(screen.queryByText('建议目标')).toBeNull()

  const proposed = '按行政区统计道路周边建筑'
  rerender(<ModelAssistPanel {...props} suggestion={{ ...suggestion, goal: { summary: proposed } }} />)
  const preview = screen.getByRole('region', { name: '建议预览' })
  expect(within(preview).getByRole('heading', { name: '建议目标' })).toBeTruthy()
  expect(within(preview).getByText(proposed)).toBeTruthy()
  fireEvent.click(within(preview).getByRole('button', { name: '应用建议' }))
  expect(props.onApply).toHaveBeenCalledTimes(1)
  expect(props.onGoal).not.toHaveBeenCalled()
  expect(screen.getByLabelText<HTMLTextAreaElement>('任务目标').value).toBe(goal)
})
