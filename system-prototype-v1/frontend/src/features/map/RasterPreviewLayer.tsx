import { useEffect, useEffectEvent, useId, useRef, useState } from 'react'
import type { ErrorEvent as MapErrorEvent, ImageSource, Map as MapLibreMap } from 'maplibre-gl'
import { ProblemDetails } from '../../components/errors/ProblemDetails'
import type { MapSession } from './MapPanel'
import { PreviewError, type PreviewBounds } from './preview'
import { fetchRasterPreview, rasterPalettes, type RasterPalette, type RasterPreview } from './raster-preview'

export interface RasterMapLayer {
  id: string
  name: string
  previewUrl: string
  kind: 'artifact'
  previewType: 'raster'
  sourceFingerprint: string
}

const resolutionFormat = new Intl.NumberFormat('zh-CN', { maximumSignificantDigits: 6, useGrouping: false })

function vectorOverlay(map: MapLibreMap): string | undefined {
  return map.getStyle().layers.find(({ type }) => type === 'fill' || type === 'line' || type === 'circle')?.id
}

function focusRaster(session: MapSession, layerId: string, bounds: PreviewBounds, fit: (session: MapSession, bounds: PreviewBounds) => void) {
  session.focusedRasterLayer = layerId
  session.map.moveLayer(layerId, vectorOverlay(session.map))
  session.rasterLayersChanged()
  fit(session, bounds)
}

export function RasterPreviewLayer({ layer, session, focusRevision, fit }: {
  layer: RasterMapLayer
  session: MapSession | null
  focusRevision?: number
  fit: (session: MapSession, bounds: PreviewBounds) => void
}) {
  const sourceId = useId()
  const layerId = `${sourceId}-raster`
  const [palette, setPalette] = useState<RasterPalette>('terrain')
  const [visible, setVisible] = useState(true)
  const [retry, setRetry] = useState(0)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<PreviewError | null>(null)
  const [display, setDisplay] = useState<{ session: MapSession; preview: Omit<RasterPreview, 'png'> } | null>(null)
  const bitmap = useRef<ImageBitmap | null>(null)
  const appliedFocus = useRef<{ session: MapSession; revision: number } | null>(null)
  const preview = display?.session === session ? display.preview : null

  const updateDrawnRaster = useEffectEvent((activeSession: MapSession, bounds: PreviewBounds) => {
    activeSession.map.setLayoutProperty(layerId, 'visibility', visible ? 'visible' : 'none')
    activeSession.rasterLayers.set(layerId, {
      artifactId: layer.id, name: layer.name, sourceFingerprint: layer.sourceFingerprint, bounds, visible,
    })
    activeSession.rasterLayersChanged()
  })

  useEffect(() => {
    if (!session?.active) return
    const { map } = session
    const failed = (event: MapErrorEvent) => {
      if ('sourceId' in event && event.sourceId === sourceId) {
        session.rasterLayers.delete(layerId)
        session.rasterLayersChanged()
        setError(new PreviewError('PREVIEW_RENDER_FAILED', '地图无法绘制此栅格预览。'))
        setLoading(false)
      }
    }
    map.on('error', failed)
    return () => {
      map.off('error', failed)
      if (session.active) {
        if (map.getLayer(layerId)) map.removeLayer(layerId)
        if (map.getSource(sourceId)) map.removeSource(sourceId)
      }
      if (session.focusedRasterLayer === layerId) session.focusedRasterLayer = undefined
      session.rasterLayers.delete(layerId)
      if (session.active) session.rasterLayersChanged()
      bitmap.current?.close()
      bitmap.current = null
    }
  }, [session, sourceId, layerId])

  useEffect(() => {
    if (!session?.active) return
    const { map } = session
    const controller = new AbortController()
    setLoading(true)
    setError(null)
    void fetchRasterPreview(layer.previewUrl, layer.id, layer.sourceFingerprint, palette, controller.signal)
      .then(async ({ png, ...result }) => {
        if (controller.signal.aborted) return
        const image = await createImageBitmap(png).catch(() => {
          throw new PreviewError('PREVIEW_INVALID_RASTER', '无法解码本机栅格预览图像。')
        })
        if (controller.signal.aborted || !session.active) {
          image.close()
          return
        }
        if (image.width !== result.width || image.height !== result.height) {
          image.close()
          throw new PreviewError('PREVIEW_INVALID_RASTER', '预览图像尺寸与本机预览记录不符。')
        }
        const newSource = !map.getSource(sourceId)
        try {
          if (newSource) map.addSource(sourceId, { type: 'image', coordinates: result.coordinates })
          map.getSource<ImageSource>(sourceId)!.updateImage({ image, coordinates: result.coordinates })
          if (!map.getLayer(layerId)) {
            const focused = session.focusedRasterLayer
            const before = focused && map.getLayer(focused) ? focused : vectorOverlay(map)
            map.addLayer({
              id: layerId, type: 'raster', source: sourceId,
              paint: { 'raster-fade-duration': 0, 'raster-resampling': 'nearest' },
            }, before)
          }
          updateDrawnRaster(session, result.bounds)
        } catch {
          if (newSource) {
            if (map.getLayer(layerId)) map.removeLayer(layerId)
            if (map.getSource(sourceId)) map.removeSource(sourceId)
          }
          image.close()
          throw new PreviewError('PREVIEW_RENDER_FAILED', '地图无法绘制此栅格预览。')
        }
        bitmap.current?.close()
        bitmap.current = image
        setDisplay({ session, preview: result })
        if (!session.fitted) fit(session, result.bounds)
        setLoading(false)
      })
      .catch((cause: unknown) => {
        if (!controller.signal.aborted) {
          setError(cause instanceof PreviewError ? cause : new PreviewError('PREVIEW_LOAD_FAILED', '无法读取本机栅格预览。'))
          setLoading(false)
        }
      })
    return () => controller.abort()
  }, [session, sourceId, layerId, layer.previewUrl, layer.id, layer.sourceFingerprint, palette, retry, fit])

  useEffect(() => {
    if (!session?.active) return
    const raster = session.rasterLayers.get(layerId)
    if (raster) updateDrawnRaster(session, raster.bounds)
  }, [session, layerId, layer.name, visible])

  useEffect(() => {
    if (focusRevision === undefined) {
      appliedFocus.current = null
      return
    }
    if (!session?.active || !preview || (appliedFocus.current?.session === session && appliedFocus.current.revision === focusRevision)) return
    appliedFocus.current = { session, revision: focusRevision }
    setVisible(true)
    focusRaster(session, layerId, preview.bounds, fit)
  }, [focusRevision, session, preview, layerId, fit])

  function locate() {
    if (!session?.active || !preview) return
    setVisible(true)
    focusRaster(session, layerId, preview.bounds, fit)
  }

  return (
    <li className="map-panel__layer" aria-label={layer.name} data-layer-id={layer.id} data-layer-kind={layer.kind}>
      <label className="map-panel__toggle">
        <input type="checkbox" aria-label={`显示 ${layer.name}`} checked={visible} disabled={!preview}
          onChange={(event) => setVisible(event.currentTarget.checked)} />
        <span className="map-panel__key" aria-hidden="true" />
        <span>{layer.name}</span>
      </label>
      <button type="button" aria-label={`定位 ${layer.name}`} disabled={!preview} onClick={locate}>定位</button>
      <label className="map-panel__palette">
        <span>配色</span>
        <select aria-label={`配色 ${layer.name}`} value={palette} onChange={(event) => setPalette(event.currentTarget.value as RasterPalette)}>
          {Object.entries(rasterPalettes).map(([value, name]) => <option key={value} value={value}>{name}</option>)}
        </select>
      </label>
      {loading && <p role="status">正在加载预览…</p>}
      {error && <div className="map-panel__layer-problem">
        <ProblemDetails problem={{
          code: error.code, message: error.message, phase: 'preview', field: 'preview',
          observed: error.httpStatus === undefined ? undefined : { http_status: error.httpStatus },
        }} actions={{ preview: () => setRetry((value) => value + 1) }} />
      </div>}
      {preview && <details className="map-panel__raster-details">
        <summary>预览 {preview.width} × {preview.height} 像元</summary>
        <dl>
          <dt>配色</dt><dd>{rasterPalettes[preview.palette]}</dd>
          <dt>原始分辨率</dt><dd>{preview.sourceResolution.map(resolutionFormat.format).join(' × ')} · {preview.sourceCrs}</dd>
          <dt>显示分辨率</dt><dd>{preview.displayResolution.map(resolutionFormat.format).join(' × ')} 米 · EPSG:3857</dd>
        </dl>
      </details>}
    </li>
  )
}
