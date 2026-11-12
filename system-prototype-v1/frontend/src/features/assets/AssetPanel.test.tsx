import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import { ApiError, type AssetEntry } from '../../api/workbench'
import { AssetPanel } from './AssetPanel'

afterEach(cleanup)

const roads: AssetEntry = {
  asset_id: 'roads-asset', asset_version: 2,
  source: { path: '/gis/sample.gpkg', layer: 'roads' },
  source_type: 'local_file', availability: 'AVAILABLE', created_at: '2026-11-11T00:00:00Z',
  metadata: { kind: 'vector', format: 'GPKG', crs: 'EPSG:32650', geometry_type: 'LineString', feature_count: 3 },
}

it('shows exact asset versions, assigned roles and controlled map visibility', () => {
  const onToggle = vi.fn()
  render(<AssetPanel
    assets={[roads]} visible={[]} assigned={{ roads }}
    onToggle={onToggle} onRegister={vi.fn()} onListLayers={vi.fn()}
  />)
  expect(screen.getByText('roads-asset')).toBeTruthy()
  expect(screen.getByText('v2')).toBeTruthy()
  expect(screen.getByText('EPSG:32650')).toBeTruthy()
  expect(screen.getByText('道路')).toBeTruthy()
  fireEvent.click(screen.getByRole('checkbox', { name: '显示 roads v2' }))
  expect(onToggle).toHaveBeenCalledWith(roads)
})

it('opens the actual layer choices before submitting an exact local registration', async () => {
  const onListLayers = vi.fn().mockResolvedValue([{ name: 'roads', geometry_type: 'LineString' }])
  const onRegister = vi.fn().mockResolvedValue(roads)
  render(<AssetPanel assets={[]} visible={[]} assigned={{}} onToggle={vi.fn()} onRegister={onRegister} onListLayers={onListLayers} />)
  fireEvent.click(screen.getByRole('button', { name: '导入数据' }))
  fireEvent.change(screen.getByLabelText('本机文件路径'), { target: { value: '/gis/sample.gpkg' } })
  fireEvent.click(screen.getByRole('button', { name: '读取图层' }))
  const layers = await screen.findByRole('combobox', { name: 'GeoPackage 图层' })
  fireEvent.change(layers, { target: { value: 'roads' } })
  fireEvent.click(screen.getByRole('button', { name: '登记资产' }))
  await waitFor(() => expect(onRegister).toHaveBeenCalledWith('/gis/sample.gpkg', 'roads'))
  expect(onListLayers).toHaveBeenCalledWith('/gis/sample.gpkg')
})

it('keeps a failing registration open and locates the server field', async () => {
  const onRegister = vi.fn().mockRejectedValue(new ApiError(422, {
    code: 'SOURCE_UNREADABLE', field: 'source.layer', message: '无法读取指定图层',
  }))
  render(<AssetPanel assets={[]} visible={[]} assigned={{}} onToggle={vi.fn()} onRegister={onRegister} onListLayers={vi.fn()} />)
  fireEvent.click(screen.getByRole('button', { name: '导入数据' }))
  fireEvent.change(screen.getByLabelText('本机文件路径'), { target: { value: '/gis/sample.gpkg' } })
  fireEvent.click(screen.getByRole('button', { name: '登记资产' }))
  const error = await screen.findByRole('alert')
  expect(within(error).getByText('无法读取指定图层')).toBeTruthy()
  expect(within(error).getByText('source.layer')).toBeTruthy()
  expect(screen.getByLabelText('本机文件路径')).toBeTruthy()
})
