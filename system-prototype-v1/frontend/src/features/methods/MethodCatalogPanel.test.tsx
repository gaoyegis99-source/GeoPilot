import { cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import type { MethodEntry } from '../../api/workbench'
import { MethodCatalogPanel } from './MethodCatalogPanel'

afterEach(cleanup)

const road: MethodEntry = {
  title: '道路缓冲统计',
  description: '计算道路影响范围，再按分区统计目标。',
  available: true,
  issues: [],
  definition: {
    id: 'road-buffer-statistics', version: 1, locked_order: true,
    inputs: { roads: 'vector_line', zones: 'vector_polygon' },
    critical_options: {}, steps: [], check_rules: [],
  },
}

const secondVersion = { ...road, definition: { ...road.definition, version: 2 } }

function expand(item: HTMLElement, version: number) {
  const summary = within(item).getByText(`版本 ${version}`).closest('summary')!
  fireEvent.click(summary)
  return summary
}

describe('MethodCatalogPanel', () => {
  it('renders the supplied versions and selects an exact method reference', () => {
    const onSelect = vi.fn()
    render(<MethodCatalogPanel methods={[road, secondVersion]} selected={null} onSelect={onSelect} />)

    const entries = screen.getAllByRole('listitem')
    expect(entries).toHaveLength(2)
    const entry = entries[1]
    expect(within(entry).getByText('road-buffer-statistics')).toBeTruthy()
    expect(within(entry).getByText('可用')).toBeTruthy()
    expand(entry, 2)
    expect(within(entry).getByText(road.description)).toBeTruthy()
    expect(within(entry).getByText('roads')).toBeTruthy()
    expect(within(entry).getByText('矢量线')).toBeTruthy()
    expect(within(entry).getByText('zones')).toBeTruthy()
    expect(within(entry).getByText('矢量面')).toBeTruthy()
    fireEvent.click(within(entry).getByRole('button', { name: '选择方法' }))
    expect(onSelect).toHaveBeenCalledExactlyOnceWith({ id: 'road-buffer-statistics', version: 2 })
  })

  it('explains an unavailable method with its real dependency diagnostic', () => {
    const onSelect = vi.fn()
    const issue = { code: 'TOOL_UNAVAILABLE', field: 'steps.0.tool', message: 'QGIS executable not found' }
    const unavailable = { ...road, available: false, issues: [issue] }
    render(<MethodCatalogPanel methods={[unavailable]} selected={null} onSelect={onSelect} />)

    const entry = screen.getByText(road.definition.id).closest('li')!
    expect(within(entry).getByText('不可用')).toBeTruthy()
    expand(entry, 1)
    expect(within(entry).getByText('执行工具不可用')).toBeTruthy()
    expect(within(entry).getByText(issue.message)).toBeTruthy()
    expect(within(entry).getByText(issue.field)).toBeTruthy()
    const button = within(entry).getByRole<HTMLButtonElement>('button', { name: '选择方法' })
    expect(button.disabled).toBe(true)
    fireEvent.click(button)
    expect(onSelect).not.toHaveBeenCalled()
  })

  it('marks only the controlled version as selected', () => {
    render(<MethodCatalogPanel methods={[road, secondVersion]} selected={{ id: road.definition.id, version: 2 }} onSelect={vi.fn()} />)

    const [first, second] = screen.getAllByRole('listitem')
    expect(within(first).getByText('可用')).toBeTruthy()
    expect(within(second).getByText('已选择')).toBeTruthy()
    expect(expand(second, 2).getAttribute('aria-current')).toBe('true')
  })

  it('shows loading, fetch error and empty directory without adding entries', () => {
    const props = { methods: [], selected: null, onSelect: vi.fn() }
    const { rerender } = render(<MethodCatalogPanel {...props} loading />)
    expect(screen.getByRole('status').textContent).toBe('正在读取方法目录…')
    expect(screen.queryByRole('listitem')).toBeNull()
    rerender(<MethodCatalogPanel {...props} error="本机服务连接失败" />)
    expect(screen.getByRole('alert').textContent).toContain('本机服务连接失败')
    rerender(<MethodCatalogPanel {...props} />)
    expect(screen.getByText('暂无已登记的方法')).toBeTruthy()
    expect(screen.queryByRole('listitem')).toBeNull()
  })
})
