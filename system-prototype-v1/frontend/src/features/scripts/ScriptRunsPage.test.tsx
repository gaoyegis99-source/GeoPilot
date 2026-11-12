import { cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import { ScriptRunsPage } from './ScriptRunsPage'

vi.mock('../map/MapPanel', () => ({
  MapPanel: ({ layers }: { layers: Array<{ name: string; previewUrl: string }> }) => (
    <div aria-label="脚本成果地图" data-preview-url={layers[0]?.previewUrl}>{layers[0]?.name}</div>
  ),
}))

const time = '2026-11-11T00:00:00Z'
const completed = {
  script_run_id: 'completed', script_id: 'filter-buildings', version: 1, created_at: time,
  status: 'SUCCEEDED', log_available: true,
}
const failed = {
  script_run_id: 'failed', script_id: 'inspect-buildings', version: 2, created_at: time,
  status: 'FAILED', log_available: true,
}
const ready = {
  script_run_id: 'ready', script_id: 'prepare-buildings', version: 1, created_at: time,
  status: 'NOT_STARTED', log_available: false,
}
const output = { index: 0, path: 'outputs/filtered.gpkg', size_bytes: 98304, readable: true, preview_available: true }
const completedDetail = {
  ...completed, exit_code: 0, timed_out: false, started_at: time, finished_at: time, error: null, outputs: [output],
}
const failedDetail = {
  ...failed, exit_code: 9, timed_out: false, started_at: time, finished_at: time, outputs: [],
  error: { code: 'PROCESS_FAILED', phase: 'execute', step_key: 'script', field: 'script',
    message: 'Script exited with code 9', suggestion: 'Review the log', log_path: 'process.log' },
}

function server(responses: Record<string, unknown>) {
  const fetch = vi.fn(async (url: string, _options?: RequestInit) => {
    const value = responses[url]
    if (typeof value === 'string') return new Response(value)
    return Response.json(value ?? { detail: { code: 'NOT_FOUND', message: 'Unknown fixture' } }, { status: value === undefined ? 404 : 200 })
  })
  vi.stubGlobal('fetch', fetch)
  return fetch
}

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
  window.history.replaceState(null, '', '/')
})

it('browses script state, actual log, indexed download and isolated vector preview', async () => {
  window.history.replaceState(null, '', '/?view=scripts&script_run=completed&task=task-1')
  const fetch = server({
    '/api/script-runs': { runs: [completed, failed] },
    '/api/script-runs/completed': completedDetail,
    '/api/script-runs/completed/log?tail=200': 'stdout:\n真实筛选完成\nexit_code: 0',
  })
  render(<ScriptRunsPage />)
  expect(screen.getByRole('heading', { name: '本地脚本运行' })).toBeTruthy()
  expect(screen.getByRole('link', { name: '返回工作台' }).getAttribute('href')).toBe('/?task=task-1')
  const manifest = await screen.findByRole('list', { name: '脚本输出' })
  expect(within(manifest).getByRole('link', { name: '下载 outputs/filtered.gpkg' }).getAttribute('href'))
    .toBe('/api/script-runs/completed/outputs/0/file')
  expect(screen.getByRole('link', { name: '下载日志' }).getAttribute('href')).toBe('/api/script-runs/completed/log')
  fireEvent.click(screen.getByRole('button', { name: '查看日志' }))
  expect(await screen.findByText(/真实筛选完成/)).toBeTruthy()
  fireEvent.click(within(manifest).getByRole('button', { name: '地图预览 outputs/filtered.gpkg' }))
  expect(screen.getByLabelText('脚本成果地图').getAttribute('data-preview-url')).toBe('/api/script-runs/completed/outputs/0/preview')
  expect(screen.queryByRole('button', { name: '开始运行' })).toBeNull()
  expect(fetch.mock.calls.every(([, options]) => !options?.method || options.method === 'GET')).toBe(true)
})

it('keeps the selected failed version, error and log separate from other script runs', async () => {
  window.history.replaceState(null, '', '/?view=scripts&script_run=failed')
  server({
    '/api/script-runs': { runs: [completed, failed] },
    '/api/script-runs/failed': failedDetail,
    '/api/script-runs/completed': completedDetail,
  })
  render(<ScriptRunsPage />)
  expect(await screen.findByText('Script exited with code 9')).toBeTruthy()
  expect(screen.getByRole('link', { name: '下载日志' }).getAttribute('href')).toBe('/api/script-runs/failed/log')
  expect(screen.queryByRole('list', { name: '脚本输出' })).toBeNull()
  fireEvent.click(screen.getByRole('button', { name: /filter-buildings · v1/ }))
  expect(await screen.findByRole('link', { name: '下载 outputs/filtered.gpkg' })).toBeTruthy()
  expect(screen.queryByText('Script exited with code 9')).toBeNull()
  expect(new URLSearchParams(window.location.search).get('script_run')).toBe('completed')
})

it('refreshes a prepared snapshot without executing and keeps unreadable output metadata visible', async () => {
  const responses: Record<string, unknown> = {
    '/api/script-runs': { runs: [ready] },
    '/api/script-runs/ready': {
      ...ready, exit_code: null, timed_out: false, started_at: null, finished_at: null, error: null, outputs: [],
    },
  }
  server(responses)
  render(<ScriptRunsPage />)
  await screen.findByRole('heading', { name: 'prepare-buildings · v1' })
  expect(screen.queryByRole('link', { name: '下载日志' })).toBeNull()
  const terminal = { ...ready, status: 'SUCCEEDED', log_available: true }
  responses['/api/script-runs'] = { runs: [terminal] }
  responses['/api/script-runs/ready'] = {
    ...completedDetail, ...terminal, outputs: [{ ...output, readable: false, preview_available: false }],
  }
  fireEvent.click(screen.getByRole('button', { name: '刷新脚本运行' }))
  const files = await screen.findByRole('list', { name: '脚本输出' })
  expect(within(files).getByText('outputs/filtered.gpkg')).toBeTruthy()
  expect(within(files).getByText('文件不可读')).toBeTruthy()
  expect(within(files).queryByRole('link')).toBeNull()
})
