import { cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'
import { WorkbenchPage } from './WorkbenchPage'

afterEach(cleanup)

describe('WorkbenchPage', () => {
  it('shows the seven regions and distinct confirmation and execution actions', () => {
    render(<WorkbenchPage />)

    expect(screen.getByRole('heading', { level: 1, name: 'GeoPilot 工作台' })).toBeTruthy()
    for (const name of ['数据', '地图', '任务', '方案', '运行', '结果', '规则检查']) {
      expect(screen.getByRole('region', { name })).toBeTruthy()
    }
    expect(within(screen.getByRole('region', { name: '地图' })).getByText('暂无图层')).toBeTruthy()
    const confirmation = within(screen.getByRole('region', { name: '方案' }))
      .getByRole<HTMLButtonElement>('button', { name: '确认方案' })
    const start = within(screen.getByRole('region', { name: '运行' }))
      .getByRole<HTMLButtonElement>('button', { name: '开始运行' })
    expect(confirmation.disabled).toBe(true)
    expect(start.disabled).toBe(true)
  })

  it('places each supplied feature within its named region', () => {
    render(<WorkbenchPage
      assets={<p>道路 GeoPackage</p>}
      map={<p>地图视口</p>}
      task={<p>道路缓冲统计</p>}
      plan={<p>缓冲距离 500 m</p>}
      run={<p>裁回分析范围</p>}
      artifacts={<p>分区统计 CSV</p>}
      checks={<p>核对输出坐标参考系</p>}
    />)

    for (const [name, content] of [
      ['数据', '道路 GeoPackage'], ['地图', '地图视口'], ['任务', '道路缓冲统计'], ['方案', '缓冲距离 500 m'],
      ['运行', '裁回分析范围'], ['结果', '分区统计 CSV'], ['规则检查', '核对输出坐标参考系'],
    ]) {
      expect(within(screen.getByRole('region', { name })).getByText(content)).toBeTruthy()
    }
  })

  it.each([
    ['WARNING', '规则检查有警告'],
    ['FAILED', '规则检查未通过'],
  ] as const)('shows %s in the closed summary independently of execution', (status, label) => {
    render(<WorkbenchPage executionStatus="SUCCEEDED" checkStatus={status} checks={<p>规则明细</p>} />)

    const execution = screen.getByRole('region', { name: '运行' })
    expect(within(execution).getByRole('status').textContent).toBe('运行成功')
    const check = screen.getByRole('region', { name: '规则检查' })
    expect(within(check).getByRole('status').textContent).toBe(label)
    const summary = within(check).getByText('规则检查').closest('summary')!
    const details = summary.parentElement as HTMLDetailsElement
    expect(details.open).toBe(false)
    fireEvent.click(summary)
    expect(details.open).toBe(true)
    expect(within(check).getByText('规则明细')).toBeTruthy()
  })
})
