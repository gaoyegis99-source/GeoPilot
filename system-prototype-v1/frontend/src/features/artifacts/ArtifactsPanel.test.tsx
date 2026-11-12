import { cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import { type ArtifactEntry, type RunArtifacts, workbenchApi } from '../../api/workbench'
import { ArtifactsPanel } from './ArtifactsPanel'

const item: ArtifactEntry = {
  id: 'artifact-1', run_id: 'run-1', role: 'filtered_features',
  uri: 'outputs/filtered.gpkg', layer: 'filtered', type: 'vector_point', kind: 'vector',
  format: 'GPKG', fingerprint: 'abc', metadata: { feature_count: 1 },
  source: { plan_id: 'plan-1', plan_version: 2, step_key: 'filter', output_name: 'filtered',
    tool: { id: 'qgis-extract-by-attribute', version: 1 }, inputs: { features: { asset_id: 'asset-1', asset_version: 3 } } },
  created_at: '2026-11-11T00:00:00Z', readable: true,
}
const record: RunArtifacts = {
  run_id: 'run-1', check_status: 'PASSED', artifacts: [item],
  diagnostics: [{ step_key: 'filter', role: 'process_log', type: 'log', path: 'steps/filter/process.log', layer: null, readable: true }],
}

afterEach(() => { cleanup(); vi.restoreAllMocks() })

it('keeps formal downloads, source versions, map selection and diagnostics tied to the same run', () => {
  const onLocate = vi.fn()
  render(<ArtifactsPanel record={record} onLocate={onLocate} />)
  const formal = screen.getByRole('list', { name: '正式成果' })
  expect(within(formal).getByText('筛选成果')).toBeTruthy()
  expect(within(formal).getByText('GPKG')).toBeTruthy()
  expect(within(formal).getByRole('link', { name: '下载原文件' }).getAttribute('href')).toBe('/api/artifacts/artifact-1/file')
  expect(within(formal).queryByText('steps/filter/process.log')).toBeNull()
  fireEvent.click(within(formal).getByText('来源详情'))
  expect(within(formal).getByText('plan-1 · v2')).toBeTruthy()
  expect(within(formal).getByText('asset-1 · v3')).toBeTruthy()
  fireEvent.click(within(formal).getByRole('button', { name: '地图定位' }))
  expect(onLocate).toHaveBeenCalledWith('artifact-1')
  fireEvent.click(screen.getByText('排错文件'))
  expect(screen.getByRole('link', { name: '打开日志' }).getAttribute('href')).toBe('/api/runs/run-1/steps/filter/log')
})

it('reads the actual formal CSV values on demand', async () => {
  const counts = { ...item, id: 'counts', role: 'counts_by_zone', format: 'CSV', kind: 'table' } as const
  const table = vi.spyOn(workbenchApi, 'artifactTable').mockResolvedValue({
    columns: ['zone_id', 'count'], rows: [['zone-west', '3']], truncated: false,
  })
  render(<ArtifactsPanel record={{ ...record, artifacts: [counts] }} />)
  fireEvent.click(screen.getByRole('button', { name: '查看统计' }))
  expect(await screen.findByRole('cell', { name: 'zone-west' })).toBeTruthy()
  expect(screen.getByRole('cell', { name: '3' })).toBeTruthy()
  expect(table).toHaveBeenCalledWith('counts')
})

it('locates a readable formal raster by artifact ID and retains the original download link', () => {
  const terrain = { ...item, id: 'registered-dem', role: 'terrain_raster', kind: 'raster', format: 'GTiff', layer: null } as const
  const onLocate = vi.fn()
  render(<ArtifactsPanel record={{ ...record, artifacts: [terrain] }} onLocate={onLocate} />)
  expect(screen.getByText('地形栅格')).toBeTruthy()
  fireEvent.click(screen.getByRole('button', { name: '地图定位' }))
  expect(onLocate).toHaveBeenCalledWith('registered-dem')
  expect(screen.getByRole('link', { name: '下载原文件' }).getAttribute('href')).toBe('/api/artifacts/registered-dem/file')
})
