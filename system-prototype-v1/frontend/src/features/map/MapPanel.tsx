import { useEffect, useId, useRef, useState } from 'react'
import {
  Map as MapLibreMap, NavigationControl, setWorkerUrl,
  type ErrorEvent as MapErrorEvent, type LayerSpecification, type MapMouseEvent, type StyleSpecification,
} from 'maplibre-gl'
import workerUrl from 'maplibre-gl/dist/maplibre-gl-worker.mjs?url'
import 'maplibre-gl/dist/maplibre-gl.css'
import { workbenchApi, type RasterValueQuery } from '../../api/workbench'
import { ProblemDetails, requestProblem } from '../../components/errors/ProblemDetails'
import { matchesPixelQuery, RasterPixelResult, type PixelLookup } from './RasterPixelResult'
import { RasterPreviewLayer, type RasterMapLayer } from './RasterPreviewLayer'
import { fetchVectorPreview, PreviewError, type PreviewBounds, type VectorPreview } from './preview'
import './map-panel.css'

setWorkerUrl(workerUrl)

interface VectorMapLayer {
  id: string
  name: string
  previewUrl: string
  kind: 'asset' | 'artifact'
  previewType?: 'vector'
}

export type MapPreviewLayer = VectorMapLayer | RasterMapLayer

export interface MapPanelProps {
  layers: readonly MapPreviewLayer[]
  focusLayerId?: string
  focusRevision?: number
  onArtifactSelect?: (artifactId: string) => void
}

export interface MapSession {
  map: MapLibreMap
  active: boolean
  fitted: boolean
  artifactIds: Map<string, string>
  focusedRasterLayer?: string
  rasterLayers: Map<string, DrawnRaster>
  rasterLayersChanged: () => void
  clearPixel: () => void
}

interface DrawnRaster {
  artifactId: string
  name: string
  sourceFingerprint: string
  bounds: PreviewBounds
  visible: boolean
}

type ClickPosition = { lng: number; lat: number }

function coversRaster(raster: DrawnRaster, position: ClickPosition): boolean {
  const [[west, south], [east, north]] = raster.bounds
  return position.lng >= west && position.lng <= east && position.lat >= south && position.lat <= north
}

function pickRaster(session: MapSession, position: ClickPosition): [string, DrawnRaster] | undefined {
  const focusedId = session.focusedRasterLayer
  const focused = focusedId ? session.rasterLayers.get(focusedId) : undefined
  if (focusedId && focused?.visible) return [focusedId, focused]
  if (!session.rasterLayers.size) return undefined
  let topmost: [string, DrawnRaster] | undefined
  for (const { id } of [...session.map.getStyle().layers].reverse()) {
    const raster = session.rasterLayers.get(id)
    if (!raster?.visible) continue
    if (coversRaster(raster, position)) return [id, raster]
    topmost ??= [id, raster]
  }
  return topmost
}

type PreviewState = { phase: 'loading' } | { phase: 'error'; error: PreviewError }
  | { phase: 'ready'; preview: VectorPreview }

const localStyle: StyleSpecification = {
  version: 8,
  sources: {},
  layers: [{ id: 'background', type: 'background', paint: { 'background-color': '#e7f1ee' } }],
}

const colors = {
  asset: { stroke: '#356879', fill: '#6294a1' },
  artifact: { stroke: '#10604e', fill: '#36a082' },
}

function styleLayers(source: string, kind: MapPreviewLayer['kind']): LayerSpecification[] {
  const color = colors[kind]
  return [
    {
      id: `${source}-fill`, type: 'fill', source,
      filter: ['==', ['geometry-type'], 'Polygon'],
      paint: { 'fill-color': color.fill, 'fill-opacity': kind === 'asset' ? 0.25 : 0.5 },
    },
    {
      id: `${source}-line`, type: 'line', source,
      filter: ['in', ['geometry-type'], ['literal', ['Polygon', 'LineString']]],
      paint: { 'line-color': color.stroke, 'line-width': kind === 'asset' ? 2 : 3 },
    },
    {
      id: `${source}-point`, type: 'circle', source,
      filter: ['==', ['geometry-type'], 'Point'],
      paint: { 'circle-color': color.stroke, 'circle-radius': 5, 'circle-stroke-color': '#fff', 'circle-stroke-width': 1.5 },
    },
  ]
}

function fit(session: MapSession, bounds: PreviewBounds) {
  if (!session.active) return
  session.map.fitBounds(bounds, { padding: 28, maxZoom: 16, duration: 0 })
  session.fitted = true
}

function locateVector(session: MapSession, bounds: PreviewBounds) {
  session.focusedRasterLayer = undefined
  session.clearPixel()
  fit(session, bounds)
}

function PreviewLayer({ layer, session, focusRevision }: {
  layer: VectorMapLayer
  session: MapSession | null
  focusRevision?: number
}) {
  const sourceId = useId()
  const [state, setState] = useState<PreviewState>({ phase: 'loading' })
  const [visible, setVisible] = useState(true)
  const [retry, setRetry] = useState(0)
  const preview = state.phase === 'ready' ? state.preview : null
  const drawable = preview?.bounds ?? null

  useEffect(() => {
    const controller = new AbortController()
    setState({ phase: 'loading' })
    fetchVectorPreview(layer.previewUrl, controller.signal)
      .then((result) => {
        if (!controller.signal.aborted) setState({ phase: 'ready', preview: result })
      })
      .catch((error: unknown) => {
        if (!controller.signal.aborted) {
          const problem = error instanceof PreviewError
            ? error : new PreviewError('PREVIEW_LOAD_FAILED', '无法读取本机图层预览。')
          setState({ phase: 'error', error: problem })
        }
      })
    return () => controller.abort()
  }, [layer.previewUrl, retry])

  useEffect(() => {
    if (!session?.active || !preview || !preview.bounds) return
    const { map, artifactIds } = session
    const definitions = styleLayers(sourceId, layer.kind)

    function removeLayers() {
      for (const definition of definitions) artifactIds.delete(definition.id)
      if (!session?.active) return
      for (const definition of definitions) if (map.getLayer(definition.id)) map.removeLayer(definition.id)
      if (map.getSource(sourceId)) map.removeSource(sourceId)
    }

    function renderFailed() {
      setState({ phase: 'error', error: new PreviewError('PREVIEW_RENDER_FAILED', '地图无法绘制此图层。') })
    }
    const sourceFailed = (event: MapErrorEvent) => {
      if ('sourceId' in event && event.sourceId === sourceId) renderFailed()
    }
    map.on('error', sourceFailed)
    try {
      map.addSource(sourceId, { type: 'geojson', data: preview.data })
      for (const definition of definitions) {
        map.addLayer(definition)
        if (layer.kind === 'artifact') artifactIds.set(definition.id, layer.id)
      }
      if (!session.fitted) fit(session, preview.bounds)
    } catch {
      renderFailed()
    }
    return () => {
      map.off('error', sourceFailed)
      removeLayers()
    }
  }, [session, preview, sourceId, layer.id, layer.kind])

  useEffect(() => {
    if (!session?.active || !drawable) return
    for (const definition of styleLayers(sourceId, layer.kind)) {
      if (session.map.getLayer(definition.id)) {
        session.map.setLayoutProperty(definition.id, 'visibility', visible ? 'visible' : 'none')
      }
    }
  }, [session, drawable, sourceId, layer.kind, visible])

  useEffect(() => {
    if (focusRevision !== undefined && session?.active && drawable) {
      setVisible(true)
      locateVector(session, drawable)
    }
  }, [focusRevision, session, drawable])

  function locate() {
    if (!session || !drawable) return
    setVisible(true)
    locateVector(session, drawable)
  }

  return (
    <li className="map-panel__layer" aria-label={layer.name} data-layer-id={layer.id} data-layer-kind={layer.kind}>
      <label className="map-panel__toggle">
        <input type="checkbox" aria-label={`显示 ${layer.name}`} checked={visible}
          disabled={!session || !drawable} onChange={(event) => setVisible(event.currentTarget.checked)} />
        <span className="map-panel__key" aria-hidden="true" />
        <span>{layer.name}</span>
      </label>
      {state.phase !== 'error' && (
        <button type="button" aria-label={`定位 ${layer.name}`} disabled={!session || !drawable} onClick={locate}>定位</button>
      )}
      {state.phase === 'loading' && <p role="status">正在加载预览…</p>}
      {state.phase === 'error' && (
        <div className="map-panel__layer-problem">
          <ProblemDetails problem={{
            code: state.error.code, message: state.error.message, phase: 'preview', field: 'preview',
            observed: state.error.httpStatus === undefined ? undefined : { http_status: state.error.httpStatus },
          }} actions={{ preview: () => setRetry((value) => value + 1) }} />
        </div>
      )}
      {state.phase === 'ready' && !drawable && <p>图层没有可绘制要素。</p>}
    </li>
  )
}

export function MapPanel({ layers, focusLayerId, focusRevision = 0, onArtifactSelect }: MapPanelProps) {
  const container = useRef<HTMLDivElement>(null)
  const onSelect = useRef(onArtifactSelect)
  const [session, setSession] = useState<MapSession | null>(null)
  const [failed, setFailed] = useState(false)
  const [retry, setRetry] = useState(0)
  const [pixel, setPixel] = useState<PixelLookup | null>(null)
  const hasLayers = layers.length > 0
  onSelect.current = onArtifactSelect

  useEffect(() => {
    setSession(null)
    setFailed(false)
    setPixel(null)
    const element = container.current
    if (!element || !hasLayers) return
    let map: MapLibreMap
    try {
      map = new MapLibreMap({
        container: element, style: localStyle, center: [0, 0], zoom: 0,
        attributionControl: false, renderWorldCopies: false, dragRotate: false, pitchWithRotate: false,
        locale: { 'NavigationControl.ZoomIn': '放大', 'NavigationControl.ZoomOut': '缩小' },
      })
    } catch {
      setFailed(true)
      return
    }
    let activeQuery: { layerId: string; sourceFingerprint: string; position: ClickPosition; controller: AbortController } | null = null
    function clearPixel() {
      if (!activeQuery) return
      activeQuery.controller.abort()
      activeQuery = null
      setPixel(null)
    }
    const next: MapSession = {
      map, active: true, fitted: false, artifactIds: new Map(), rasterLayers: new Map(), clearPixel,
      rasterLayersChanged: () => {
        if (!activeQuery) return
        const target = pickRaster(next, activeQuery.position)
        if (target?.[0] !== activeQuery.layerId || target?.[1].sourceFingerprint !== activeQuery.sourceFingerprint) clearPixel()
      },
    }
    let loaded = false
    const ready = () => {
      if (!next.active) return
      loaded = true
      setFailed(false)
      setSession(next)
    }
    const initializationFailed = () => {
      if (next.active && !loaded) setFailed(true)
    }
    function readPixel(layerId: string, raster: DrawnRaster, position: ClickPosition) {
      activeQuery?.controller.abort()
      const controller = new AbortController()
      const query = { layerId, sourceFingerprint: raster.sourceFingerprint, position, controller }
      activeQuery = query
      const target = { artifactId: raster.artifactId, artifactName: raster.name }
      const coordinates: RasterValueQuery = {
        longitude: position.lng, latitude: position.lat, band: 1, source_fingerprint: raster.sourceFingerprint,
      }
      setPixel({ ...target, phase: 'loading' })
      onSelect.current?.(raster.artifactId)
      void workbenchApi.artifactValue(raster.artifactId, coordinates, controller.signal)
        .then((result) => {
          if (activeQuery !== query || controller.signal.aborted) return
          if (matchesPixelQuery(result, raster.artifactId, coordinates)) {
            setPixel({ ...target, phase: 'ready', result })
          } else {
            setPixel({ ...target, phase: 'error', problem: {
              phase: 'preview', code: 'RASTER_VALUE_INVALID', message: '原始像元查询结果与当前成果或坐标不符。',
            } })
          }
        })
        .catch((cause: unknown) => {
          if (activeQuery === query && !controller.signal.aborted) {
            setPixel({ ...target, phase: 'error', problem: requestProblem(cause, 'preview', '无法读取原始像元。') })
          }
        })
    }
    function pick(event: MapMouseEvent) {
      if (!loaded) return
      const position = event.lngLat
      const raster = position && Number.isFinite(position.lng) && Number.isFinite(position.lat)
        && Math.abs(position.lng) <= 180 && Math.abs(position.lat) <= 90 ? pickRaster(next, position) : undefined
      if (raster && raster[0] === next.focusedRasterLayer && coversRaster(raster[1], position)) {
        readPixel(...raster, position)
        return
      }
      if (onSelect.current) {
        const candidates = [...next.artifactIds.keys()].filter((id) => map.getLayer(id))
        const feature = candidates.length ? map.queryRenderedFeatures(event.point, { layers: candidates })[0] : undefined
        const artifactId = feature ? next.artifactIds.get(feature.layer.id) : undefined
        if (artifactId !== undefined) {
          clearPixel()
          onSelect.current(artifactId)
          return
        }
      }
      if (raster) readPixel(...raster, position)
      else clearPixel()
    }
    map.on('load', ready)
    map.on('error', initializationFailed)
    map.on('click', pick)
    map.addControl(new NavigationControl({ showCompass: false }), 'top-right')
    const observer = typeof ResizeObserver === 'undefined' ? null : new ResizeObserver(() => {
      if (next.active) map.resize()
    })
    observer?.observe(element)
    return () => {
      next.active = false
      activeQuery?.controller.abort()
      activeQuery = null
      observer?.disconnect()
      map.remove()
    }
  }, [hasLayers, retry])

  return (
    <div className="map-panel">
      <div className="map-panel__viewport">
        <div ref={container} className="map-panel__canvas" aria-label="本机地图预览" />
        {!hasLayers && <p className="map-panel__notice">暂无图层</p>}
        {hasLayers && !session && !failed && <p className="map-panel__notice" role="status">正在初始化地图…</p>}
        {hasLayers && failed && (
          <div className="map-panel__notice">
            <p className="map-panel__error" role="alert">无法初始化地图。</p>
            <button type="button" onClick={() => setRetry((value) => value + 1)}>重试地图</button>
          </div>
        )}
      </div>
      {pixel && <RasterPixelResult lookup={pixel} />}
      {hasLayers && (
        <ul className="map-panel__layers" aria-label="地图图层">
          {layers.map((layer) => {
            const focus = focusLayerId === layer.id ? focusRevision : undefined
            return layer.previewType === 'raster'
              ? <RasterPreviewLayer key={JSON.stringify([layer.id, layer.previewUrl, layer.sourceFingerprint])}
                  layer={layer} session={session} focusRevision={focus} fit={fit} />
              : <PreviewLayer key={JSON.stringify([layer.id, layer.previewUrl])}
                  layer={layer} session={session} focusRevision={focus} />
          })}
        </ul>
      )}
    </div>
  )
}
