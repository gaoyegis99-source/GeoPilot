import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import { ConfirmPlanButton, StartPlanButton } from './PlanActions'

afterEach(cleanup)

const draft = { id: 'plan-a', version: 3, status: 'draft', issues: [] } as const
const confirmed = { ...draft, status: 'confirmed' } as const

it('confirms the exact draft without starting its execution', () => {
  const confirm = vi.fn()
  const start = vi.fn()
  const { rerender } = render(<>
    <ConfirmPlanButton plan={draft} onConfirm={confirm} />
    <StartPlanButton plan={draft} onStart={start} />
  </>)
  fireEvent.click(screen.getByRole('button', { name: '开始运行' }))
  expect(start).not.toHaveBeenCalled()
  fireEvent.click(screen.getByRole('button', { name: '确认方案' }))
  expect(confirm).toHaveBeenCalledExactlyOnceWith({ id: 'plan-a', version: 3 })
  expect(start).not.toHaveBeenCalled()

  rerender(<>
    <ConfirmPlanButton plan={confirmed} onConfirm={confirm} />
    <StartPlanButton plan={confirmed} onStart={start} />
  </>)
  fireEvent.click(screen.getByRole('button', { name: '开始运行' }))
  expect(start).toHaveBeenCalledExactlyOnceWith({ id: 'plan-a', version: 3 })
})

it('blocks unresolved, edited and pending snapshots', () => {
  const confirm = vi.fn()
  const start = vi.fn()
  const { rerender } = render(
    <ConfirmPlanButton plan={{ ...draft, issues: [{ code: 'REQUIRED', field: 'inputs.features', message: '待选择' }] }} onConfirm={confirm} />,
  )
  fireEvent.click(screen.getByRole('button', { name: '确认方案' }))
  rerender(<>
    <ConfirmPlanButton plan={draft} dirty onConfirm={confirm} />
    <StartPlanButton plan={confirmed} dirty onStart={start} />
  </>)
  fireEvent.click(screen.getByRole('button', { name: '确认方案' }))
  fireEvent.click(screen.getByRole('button', { name: '开始运行' }))
  rerender(<StartPlanButton plan={confirmed} busy onStart={start} />)
  fireEvent.click(screen.getByRole('button', { name: '开始运行' }))
  expect(confirm).not.toHaveBeenCalled()
  expect(start).not.toHaveBeenCalled()
})
