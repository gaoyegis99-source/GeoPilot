import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { MapPanel, type MapPreviewLayer } from './MapPanel'

const maps = vi.hoisted(() => {
  type Handler = (event?: unknown) => void
  const instances: FakeMap[] = []

  class FakeMap {
    sources = new globalThis.Map<string, object>()
    layers = new globalThis.Map<string, { id: string; type: string; source?: string }>()
    handlers = new globalThis.Map<string, Set<Handler>>()
    fitBounds = vi.fn()
    setLayoutProperty = vi.fn()
    queryRenderedFeatures = vi.fn().mockReturnValue([])
    updateImage = vi.fn()
    resize = vi.fn()
    addControl = vi.fn()
    remove = vi.fn()

    constructor(readonly options: { style: { sources: object } }) {
      instances.push(this)
      queueMicrotask(() => this.emit('load'))
    }

    on(event: string, handler: Handler) {
      const handlers = this.handlers.get(event) ?? new Set<Handler>()
      handlers.add(handler)
      this.handlers.set(event, handlers)
      return this
    }

    off(event: string, handler: Handler) {
      this.handlers.get(event)?.delete(handler)
      return this
    }

    emit(event: string, value?: unknown) {
      this.handlers.get(event)?.forEach((handler) => handler(value))
    }

    addSource(id: string, source: object) {
      this.sources.set(id, 'type' in source && source.type === 'image' ? { ...source, updateImage: this.updateImage } : source)
    }

    getSource(id: string) {
      return this.sources.get(id)
    }

    removeSource(id: string) {
      this.sources.delete(id)
    }

    addLayer(layer: { id: string; type: string; source?: string }, before?: string) {
      const entries = [...this.layers.entries()]
      const position = entries.findIndex(([id]) => id === before)
      if (position >= 0) {
        entries.splice(position, 0, [layer.id, layer])
        this.layers = new globalThis.Map(entries)
        return
      }
      this.layers.set(layer.id, layer)
    }

    getStyle() {
      return { layers: [...this.layers.values()] }
    }

    moveLayer(id: string, before?: string) {
      const layer = this.layers.get(id)
      if (layer) {
        this.layers.delete(id)
        this.addLayer(layer, before)
      }
    }

    getLayer(id: string) {
      return this.layers.get(id)
    }

    removeLayer(id: string) {
      this.layers.delete(id)
    }
  }

  return { FakeMap, instances }
})

vi.mock('maplibre-gl', () => ({
  Map: maps.FakeMap,
  NavigationControl: class NavigationControl {},
  setWorkerUrl: vi.fn(),
}))

const asset: MapPreviewLayer = {
  id: 'input-a@2', name: '分析范围', previewUrl: '/api/assets/input-a/preview?version=2', kind: 'asset',
}
const artifact: MapPreviewLayer = {
  id: 'artifact-exact-id', name: '真实成果', previewUrl: '/api/artifacts/artifact-exact-id/preview', kind: 'artifact',
}
const raster = {
  id: 'registered-raster', name: '地形栅格', previewUrl: '/api/artifacts/registered-raster/raster-preview', kind: 'artifact',
  previewType: 'raster', sourceFingerprint: 'a'.repeat(64),
} as const
const geometry = {
  type: 'FeatureCollection',
  features: [
    { type: 'Feature', properties: { artifact_id: 'data-property-must-not-win' },
      geometry: { type: 'Point', coordinates: [117.1, 39.1] } },
    { type: 'Feature', properties: {},
      geometry: { type: 'LineString', coordinates: [[117, 39], [117.2, 39.2]] } },
    { type: 'Feature', properties: {},
      geometry: { type: 'Polygon', coordinates: [[[117, 39], [117.2, 39], [117.2, 39.2], [117, 39]]] } },
  ],
}

function jsonResponse(body: object, status = 200) {
  return new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/geo+json' } })
}

function descriptor(palette = 'terrain', overrides: object = {}, layer: {
  id: string; previewUrl: string; sourceFingerprint: string
} = raster) {
  const imageQuery = new URLSearchParams({ palette, max_size: '1024', band: '1', source_fingerprint: layer.sourceFingerprint })
  return {
    artifact_id: layer.id, source_fingerprint: layer.sourceFingerprint, image_url: `${layer.previewUrl}.png?${imageQuery}`,
    coordinates: [[117, 39.2], [117.2, 39.2], [117.2, 39], [117, 39]],
    width: 3, height: 2, band: 1, palette, color_mode: 'continuous',
    source_crs: 'EPSG:32650', source_resolution: [10, 10], display_crs: 'EPSG:3857', display_resolution: [12, 12],
    resampling: 'nearest', ramp: [{ position: 0, rgba: [0, 0, 0, 255] }, { position: 1, rgba: [255, 255, 255, 255] }],
    stretch: { minimum: 10, maximum: 50 }, classes: [], nodata_alpha: 0, ...overrides,
  }
}

function pngResponse() {
  return new Response(new Uint8Array([137, 80, 78, 71]), { headers: { 'content-type': 'image/png' } })
}

function imageBitmap() {
  return { width: 3, height: 2, close: vi.fn() }
}

const thresholdRaster = {
  ...raster, id: 'registered-threshold', name: '阈值区域', previewUrl: '/api/artifacts/registered-threshold/raster-preview',
  sourceFingerprint: 'c'.repeat(64),
} as const
const precisePoint = { lng: 117.10000000000003, lat: 39.100000000000016 }

function rasterResponse(url: URL) {
  if (url.pathname.endsWith('.png')) return pngResponse()
  const layer = [raster, thresholdRaster].find((candidate) => url.pathname === candidate.previewUrl)
  if (layer) return jsonResponse(descriptor(url.searchParams.get('palette') ?? 'terrain', {}, layer))
  return jsonResponse(geometry)
}

function pixelResponse(url: URL, overrides: object = {}) {
  return jsonResponse({
    artifact_id: url.pathname.split('/')[3], source_fingerprint: url.searchParams.get('source_fingerprint'),
    longitude: Number(url.searchParams.get('longitude')), latitude: Number(url.searchParams.get('latitude')),
    band: 1, source_crs: 'EPSG:32650', source_x: 500035, source_y: 4400035, row: 1, column: 1,
    status: 'value', nodata: false, value: 0, dtype: 'float32', unit: 'degree', scale: 1, offset: 0,
    ...overrides,
  })
}

beforeEach(() => {
  maps.instances.length = 0
})

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
})

it('uses local source references, renders all three vector styles and picks the exact artifact ID', async () => {
  const request = vi.fn(async (_url: string | URL) => jsonResponse(geometry))
  vi.stubGlobal('fetch', request)
  const onArtifactSelect = vi.fn()
  const screenResult = render(<MapPanel layers={[asset, artifact]} onArtifactSelect={onArtifactSelect} />)
  await waitFor(() => expect(maps.instances[0]?.sources.size).toBe(2))
  const map = maps.instances[0]
  expect(map.options.style.sources).toEqual({})
  expect(request.mock.calls.map(([url]) => String(url))).toEqual([
    expect.stringContaining(asset.previewUrl), expect.stringContaining(artifact.previewUrl),
  ])
  const artifactLayers = [...map.layers.values()].slice(-3)
  expect(artifactLayers.map((layer) => layer.type)).toEqual(['fill', 'line', 'circle'])
  map.queryRenderedFeatures.mockReturnValue([{ layer: { id: artifactLayers[2].id }, properties: {
    artifact_id: 'data-property-must-not-win',
  } }])
  act(() => map.emit('click', { point: { x: 12, y: 15 } }))
  expect(onArtifactSelect).toHaveBeenCalledWith(artifact.id)
  expect(map.queryRenderedFeatures.mock.calls[0][1].layers).toEqual(artifactLayers.map(({ id }) => id))

  fireEvent.click(screen.getByRole('checkbox', { name: '显示 真实成果' }))
  expect(map.setLayoutProperty).toHaveBeenCalledWith(artifactLayers[2].id, 'visibility', 'none')
  fireEvent.click(screen.getByRole('button', { name: '定位 真实成果' }))
  expect(map.fitBounds).toHaveBeenLastCalledWith([[117, 39], [117.2, 39.2]], expect.any(Object))
  expect(map.setLayoutProperty).toHaveBeenLastCalledWith(artifactLayers[2].id, 'visibility', 'visible')
  screenResult.rerender(<MapPanel layers={[asset, artifact]} focusLayerId={artifact.id} focusRevision={0} />)
  fireEvent.click(screen.getByRole('checkbox', { name: '显示 真实成果' }))
  map.fitBounds.mockClear()
  screenResult.rerender(<MapPanel layers={[asset, artifact]} focusLayerId={artifact.id} focusRevision={1} />)
  expect(map.fitBounds).toHaveBeenCalledTimes(1)
  expect(map.setLayoutProperty).toHaveBeenLastCalledWith(artifactLayers[2].id, 'visibility', 'visible')
  screenResult.unmount()
  expect(map.remove).toHaveBeenCalledTimes(1)
})

it('keeps the usable preview, download and log while retrying only the failed layer', async () => {
  let failed = true
  const request = vi.fn(async (url: string | URL) => String(url).includes(artifact.previewUrl) && failed
    ? jsonResponse({}, 422) : jsonResponse(geometry))
  vi.stubGlobal('fetch', request)
  render(
    <>
      <a href="/api/artifacts/artifact-exact-id/file">下载成果</a>
      <a href="/api/runs/run-1/steps/step-1/log">查看日志</a>
      <MapPanel layers={[asset, artifact]} />
    </>,
  )
  await screen.findByRole('alert')
  const map = maps.instances[0]
  await waitFor(() => expect(map.sources.size).toBe(1))
  expect(screen.getByRole('alert').textContent).toContain('422')
  const failedLayer = within(screen.getByRole('listitem', { name: '真实成果' }))
  expect(failedLayer.getByText('技术详情').closest('details')?.open).toBe(false)
  expect(failedLayer.getByText('PREVIEW_LOAD_FAILED')).toBeTruthy()
  expect(screen.getByRole('link', { name: '下载成果' }).getAttribute('href')).toBe('/api/artifacts/artifact-exact-id/file')
  expect(screen.getByRole('link', { name: '查看日志' })).toBeTruthy()
  failed = false
  fireEvent.click(failedLayer.getByRole('button', { name: '重新加载预览' }))
  await waitFor(() => expect(map.sources.size).toBe(2))
  expect(screen.queryByRole('alert')).toBeNull()
  expect(request.mock.calls.filter(([url]) => String(url).includes(asset.previewUrl))).toHaveLength(1)
})

it('cancels an obsolete request and uses the bounds of the requested version', async () => {
  let finishOld: ((response: Response) => void) | undefined
  const request = vi.fn(async (url: string | URL, options?: RequestInit) => {
    if (String(url).includes('version=1')) {
      return new Promise<Response>((resolve) => { finishOld = resolve })
    }
    expect(options?.signal?.aborted).toBe(false)
    return jsonResponse(geometry)
  })
  vi.stubGlobal('fetch', request)
  const result = render(<MapPanel layers={[{ ...asset, previewUrl: '/api/assets/input-a/preview?version=1' }]} />)
  await waitFor(() => expect(finishOld).toBeDefined())
  result.rerender(<MapPanel layers={[asset]} focusLayerId={asset.id} />)
  await waitFor(() => expect(maps.instances[0]?.sources.size).toBe(1))
  expect(request.mock.calls[0][1]?.signal?.aborted).toBe(true)
  await act(async () => finishOld?.(jsonResponse({ type: 'FeatureCollection', features: [] })))
  expect(maps.instances[0].sources.size).toBe(1)
  expect(maps.instances[0].fitBounds).toHaveBeenLastCalledWith([[117, 39], [117.2, 39.2]], expect.any(Object))
})

it('handles an empty preview and rejects an external preview URL without sending a request', async () => {
  const request = vi.fn(async () => jsonResponse({ type: 'FeatureCollection', features: [] }))
  vi.stubGlobal('fetch', request)
  render(<MapPanel layers={[asset, { ...artifact, previewUrl: 'https://example.com/external.geojson' }]} />)
  await screen.findByText('图层没有可绘制要素。')
  await screen.findByRole('alert')
  expect(request).toHaveBeenCalledTimes(1)
  expect(maps.instances[0]?.sources.size).toBe(0)
})

it('draws the exact registered raster under vector layers and keeps locate and visibility controls', async () => {
  const bitmap = imageBitmap()
  vi.stubGlobal('createImageBitmap', vi.fn(async () => bitmap))
  const request = vi.fn(async (url: URL) => {
    if (url.pathname.endsWith('.png')) return pngResponse()
    if (url.pathname === raster.previewUrl) return jsonResponse(descriptor())
    return jsonResponse(geometry)
  })
  vi.stubGlobal('fetch', request)
  const result = render(<MapPanel layers={[asset, raster]} />)

  await waitFor(() => expect(maps.instances[0]?.updateImage).toHaveBeenCalled())
  const map = maps.instances[0]
  expect(maps.instances).toHaveLength(1)
  expect([...map.layers.values()].map(({ type }) => type)).toEqual(['raster', 'fill', 'line', 'circle'])
  expect([...map.sources.values()]).toEqual(expect.arrayContaining([
    expect.objectContaining({ type: 'image', coordinates: descriptor().coordinates }),
  ]))
  const layerId = [...map.layers.values()].find(({ type }) => type === 'raster')!.id
  const row = within(screen.getByRole('listitem', { name: '地形栅格' }))
  expect(screen.getByRole('listitem', { name: '地形栅格' }).getAttribute('data-layer-id')).toBe(raster.id)
  expect(row.getByRole<HTMLSelectElement>('combobox', { name: '配色 地形栅格' }).value).toBe('terrain')
  expect(row.getByText('预览 3 × 2 像元')).toBeTruthy()
  expect(request.mock.calls.map(([url]) => url.pathname)).toEqual(expect.arrayContaining([
    raster.previewUrl, `${raster.previewUrl}.png`, asset.previewUrl.split('?')[0],
  ]))
  fireEvent.click(row.getByRole('checkbox', { name: '显示 地形栅格' }))
  expect(map.setLayoutProperty).toHaveBeenLastCalledWith(layerId, 'visibility', 'none')
  fireEvent.click(row.getByRole('button', { name: '定位 地形栅格' }))
  expect(map.setLayoutProperty).toHaveBeenLastCalledWith(layerId, 'visibility', 'visible')
  expect(map.fitBounds).toHaveBeenLastCalledWith([[117, 39], [117.2, 39.2]], expect.any(Object))
  result.unmount()
  expect(bitmap.close).toHaveBeenCalledTimes(1)
})

it('keeps the newer palette and disposes a bitmap decoded after its request became obsolete', async () => {
  const first = imageBitmap()
  const stale = imageBitmap()
  const current = imageBitmap()
  let finishStale: ((bitmap: ReturnType<typeof imageBitmap>) => void) | undefined
  const decode = vi.fn()
    .mockResolvedValueOnce(first)
    .mockImplementationOnce(() => new Promise((resolve) => { finishStale = resolve }))
    .mockResolvedValueOnce(current)
  vi.stubGlobal('createImageBitmap', decode)
  const request = vi.fn(async (url: URL, _options?: RequestInit) => url.pathname.endsWith('.png')
    ? pngResponse() : jsonResponse(descriptor(url.searchParams.get('palette') ?? 'terrain')))
  vi.stubGlobal('fetch', request)
  render(<MapPanel layers={[raster]} focusLayerId={raster.id} />)

  await waitFor(() => expect(maps.instances[0]?.updateImage).toHaveBeenCalledTimes(1))
  const map = maps.instances[0]
  const palette = screen.getByRole('combobox', { name: '配色 地形栅格' })
  map.fitBounds.mockClear()
  fireEvent.change(palette, { target: { value: 'grayscale' } })
  await waitFor(() => expect(finishStale).toBeDefined())
  expect(map.updateImage).toHaveBeenCalledTimes(1)
  fireEvent.change(palette, { target: { value: 'terrain' } })
  await waitFor(() => expect(map.updateImage).toHaveBeenCalledTimes(2))
  expect(map.updateImage).toHaveBeenLastCalledWith(expect.objectContaining({ image: current }))
  expect(request.mock.calls.find(([url]) => url.pathname.endsWith('.png') && url.searchParams.get('palette') === 'grayscale')?.[1]?.signal?.aborted).toBe(true)
  await act(async () => finishStale?.(stale))
  expect(map.updateImage).toHaveBeenCalledTimes(2)
  expect(stale.close).toHaveBeenCalledTimes(1)
  expect(first.close).toHaveBeenCalledTimes(1)
  expect(map.fitBounds).not.toHaveBeenCalled()
})

it('keeps a located raster on top when another raster finishes loading later', async () => {
  const other = { ...raster, id: 'late-threshold', name: '阈值区域', previewUrl: '/api/artifacts/late-threshold/raster-preview' }
  let finishOther: ((response: Response) => void) | undefined
  vi.stubGlobal('createImageBitmap', vi.fn(async () => imageBitmap()))
  vi.stubGlobal('fetch', vi.fn(async (url: URL) => {
    if (url.pathname.endsWith('.png')) return pngResponse()
    if (url.pathname === raster.previewUrl) return jsonResponse(descriptor())
    return new Promise<Response>((resolve) => { finishOther = resolve })
  }))
  render(<MapPanel layers={[raster, other]} />)
  await waitFor(() => expect(maps.instances[0]?.updateImage).toHaveBeenCalledTimes(1))
  const map = maps.instances[0]
  const [located] = map.getStyle().layers
  fireEvent.click(screen.getByRole('button', { name: '定位 地形栅格' }))
  await act(async () => finishOther?.(jsonResponse(descriptor('terrain', {}, other))))
  await waitFor(() => expect(map.updateImage).toHaveBeenCalledTimes(2))
  expect(map.getStyle().layers.at(-1)?.id).toBe(located.id)
})

it('retries a failed raster PNG locally without reloading the usable vector preview', async () => {
  vi.stubGlobal('createImageBitmap', vi.fn(async () => imageBitmap()))
  let failed = true
  const request = vi.fn(async (url: URL) => {
    if (url.pathname.endsWith('.png')) return failed ? jsonResponse({}, 422) : pngResponse()
    if (url.pathname === raster.previewUrl) return jsonResponse(descriptor())
    return jsonResponse(geometry)
  })
  vi.stubGlobal('fetch', request)
  render(<MapPanel layers={[asset, raster]} />)
  const row = within(screen.getByRole('listitem', { name: '地形栅格' }))
  await row.findByRole('alert')
  expect([...maps.instances[0].layers.values()].map(({ type }) => type)).toEqual(['fill', 'line', 'circle'])

  failed = false
  fireEvent.click(row.getByRole('button', { name: '重新加载预览' }))
  await waitFor(() => expect(maps.instances[0].updateImage).toHaveBeenCalledTimes(1))
  expect(row.queryByRole('alert')).toBeNull()
  expect(request.mock.calls.filter(([url]) => url.pathname === '/api/assets/input-a/preview')).toHaveLength(1)
})

it.each([
  [{ artifact_id: 'a-different-artifact' }, 'PREVIEW_INVALID_RASTER'],
  [{ source_fingerprint: 'b'.repeat(64) }, 'PREVIEW_INVALID_RASTER'],
  [{ image_url: 'https://example.com/external.png' }, 'PREVIEW_ADDRESS_INVALID'],
])('rejects a mismatched raster descriptor or image address before fetching image bytes', async (overrides, error) => {
  const request = vi.fn(async () => jsonResponse(descriptor('terrain', overrides)))
  vi.stubGlobal('fetch', request)
  render(<MapPanel layers={[raster]} />)
  await screen.findByRole('alert')
  expect(screen.getByText(error)).toBeTruthy()
  expect(request).toHaveBeenCalledTimes(1)
  expect(maps.instances[0].sources.size).toBe(0)
})

it('reads the located formal raster using the exact source and click coordinates, including zero', async () => {
  vi.stubGlobal('createImageBitmap', vi.fn(async () => imageBitmap()))
  const values: URL[] = []
  vi.stubGlobal('fetch', vi.fn(async (address: string | URL) => {
    const url = new URL(address, window.location.href)
    if (!url.pathname.endsWith('/value')) return rasterResponse(url)
    values.push(url)
    return pixelResponse(url)
  }))
  const onArtifactSelect = vi.fn()
  render(<MapPanel layers={[artifact, raster, thresholdRaster]} onArtifactSelect={onArtifactSelect} />)
  await waitFor(() => expect(maps.instances[0]?.updateImage).toHaveBeenCalledTimes(2))
  const map = maps.instances[0]
  const vector = [...map.layers.values()].find(({ type }) => type === 'circle')!
  map.queryRenderedFeatures.mockReturnValue([{ layer: { id: vector.id } }])
  act(() => map.emit('click', { point: { x: 12, y: 15 }, lngLat: precisePoint }))
  expect(onArtifactSelect).toHaveBeenLastCalledWith(artifact.id)
  expect(values).toHaveLength(0)

  fireEvent.click(screen.getByRole('button', { name: '定位 地形栅格' }))
  act(() => map.emit('click', { point: { x: 12, y: 15 }, lngLat: precisePoint }))

  const detail = within(await screen.findByRole('region', { name: '原始像元' }))
  expect(await detail.findByText('0°')).toBeTruthy()
  expect(values).toHaveLength(1)
  expect(values[0].pathname).toBe(`/api/artifacts/${raster.id}/value`)
  expect(Object.fromEntries(values[0].searchParams)).toEqual({
    longitude: String(precisePoint.lng), latitude: String(precisePoint.lat), band: '1', source_fingerprint: raster.sourceFingerprint,
  })
  expect(onArtifactSelect).toHaveBeenLastCalledWith(raster.id)
  expect(detail.getByText('500035, 4400035 · EPSG:32650')).toBeTruthy()
  expect(detail.getByText('行')).toBeTruthy()
  expect(detail.getByText('列')).toBeTruthy()
  fireEvent.change(screen.getByRole('combobox', { name: '配色 地形栅格' }), { target: { value: 'grayscale' } })
  await waitFor(() => expect(map.updateImage).toHaveBeenCalledTimes(3))
  expect(values).toHaveLength(1)
  expect(detail.getByText('0°')).toBeTruthy()

  fireEvent.click(screen.getByRole('button', { name: '定位 真实成果' }))
  expect(screen.queryByRole('region', { name: '原始像元' })).toBeNull()
  act(() => map.emit('click', { point: { x: 12, y: 15 }, lngLat: precisePoint }))
  expect(onArtifactSelect).toHaveBeenLastCalledWith(artifact.id)
  expect(values).toHaveLength(1)
})

it('uses the actual topmost visible raster when no raster has been located', async () => {
  vi.stubGlobal('createImageBitmap', vi.fn(async () => imageBitmap()))
  let latestValue: URL | undefined
  vi.stubGlobal('fetch', vi.fn(async (address: string | URL) => {
    const url = new URL(address, window.location.href)
    if (!url.pathname.endsWith('/value')) return rasterResponse(url)
    latestValue = url
    return pixelResponse(url)
  }))
  const active = render(<MapPanel layers={[raster]} />)
  await waitFor(() => expect(maps.instances[0]?.updateImage).toHaveBeenCalledTimes(1))
  active.rerender(<MapPanel layers={[raster, thresholdRaster]} />)
  await waitFor(() => expect(maps.instances[0].updateImage).toHaveBeenCalledTimes(2))
  const map = maps.instances[0]
  act(() => map.emit('click', { point: { x: 12, y: 15 }, lngLat: precisePoint }))
  await waitFor(() => expect(latestValue?.pathname).toBe(`/api/artifacts/${thresholdRaster.id}/value`))
  expect(latestValue?.searchParams.get('source_fingerprint')).toBe(thresholdRaster.sourceFingerprint)

  fireEvent.click(screen.getByRole('checkbox', { name: '显示 阈值区域' }))
  act(() => map.emit('click', { point: { x: 12, y: 15 }, lngLat: precisePoint }))
  await waitFor(() => expect(latestValue?.pathname).toBe(`/api/artifacts/${raster.id}/value`))
})

it('aborts obsolete clicks and a replaced raster selection without displaying their late values', async () => {
  vi.stubGlobal('createImageBitmap', vi.fn(async () => imageBitmap()))
  const pending: Array<{ url: URL; signal?: AbortSignal | null; resolve: (response: Response) => void }> = []
  vi.stubGlobal('fetch', vi.fn(async (address: string | URL, options?: RequestInit) => {
    const url = new URL(address, window.location.href)
    if (!url.pathname.endsWith('/value')) return rasterResponse(url)
    return new Promise<Response>((resolve) => pending.push({ url, signal: options?.signal, resolve }))
  }))
  render(<MapPanel layers={[raster, thresholdRaster]} />)
  await waitFor(() => expect(maps.instances[0]?.updateImage).toHaveBeenCalledTimes(2))
  const map = maps.instances[0]
  fireEvent.click(screen.getByRole('button', { name: '定位 地形栅格' }))
  act(() => map.emit('click', { point: { x: 12, y: 15 }, lngLat: precisePoint }))
  await waitFor(() => expect(pending).toHaveLength(1))
  act(() => map.emit('click', { point: { x: 13, y: 15 }, lngLat: { ...precisePoint, lng: 117.11 } }))
  await waitFor(() => expect(pending).toHaveLength(2))
  expect(pending[0].signal?.aborted).toBe(true)
  await act(async () => pending[0].resolve(pixelResponse(pending[0].url, { value: 12 })))
  expect(screen.queryByText('12°')).toBeNull()

  fireEvent.click(screen.getByRole('button', { name: '定位 阈值区域' }))
  expect(pending[1].signal?.aborted).toBe(true)
  await act(async () => pending[1].resolve(pixelResponse(pending[1].url, { value: 45 })))
  expect(screen.queryByText('45°')).toBeNull()
  act(() => map.emit('click', { point: { x: 12, y: 15 }, lngLat: precisePoint }))
  await waitFor(() => expect(pending).toHaveLength(3))
  expect(pending[2].url.pathname).toBe(`/api/artifacts/${thresholdRaster.id}/value`)
  await act(async () => pending[2].resolve(pixelResponse(pending[2].url, { value: 7, unit: 'class', dtype: 'uint8' })))
  expect(within(screen.getByRole('region', { name: '原始像元' })).getByText('7 · 类别')).toBeTruthy()
})

it.each([
  [{ status: 'nodata', nodata: true, value: null }, '无数据（NoData）'],
  [{ status: 'outside', row: null, column: null, value: null }, '范围外'],
  [{ value: '18446744073709551615', unit: null, dtype: 'uint64' }, '18446744073709551615'],
])('shows distinct raw pixel states and preserves a large integer string', async (data, label) => {
  vi.stubGlobal('createImageBitmap', vi.fn(async () => imageBitmap()))
  vi.stubGlobal('fetch', vi.fn(async (address: string | URL) => {
    const url = new URL(address, window.location.href)
    return url.pathname.endsWith('/value') ? pixelResponse(url, data) : rasterResponse(url)
  }))
  render(<MapPanel layers={[raster]} />)
  await waitFor(() => expect(maps.instances[0]?.updateImage).toHaveBeenCalledTimes(1))
  const lngLat = 'status' in data && data.status === 'outside' ? { lng: 117.3, lat: 39.1 } : precisePoint
  act(() => maps.instances[0].emit('click', { point: { x: 12, y: 15 }, lngLat }))
  expect(await within(await screen.findByRole('region', { name: '原始像元' })).findByText(label)).toBeTruthy()
})
