import type { FeatureCollection } from 'geojson'

export type PreviewBounds = [[number, number], [number, number]]

export interface VectorPreview {
  data: FeatureCollection
  bounds: PreviewBounds | null
}

type PreviewErrorCode = 'PREVIEW_ADDRESS_INVALID' | 'PREVIEW_LOAD_FAILED'
  | 'PREVIEW_INVALID_GEOJSON' | 'PREVIEW_INVALID_RASTER' | 'PREVIEW_RENDER_FAILED'

export class PreviewError extends Error {
  constructor(readonly code: PreviewErrorCode, message: string, readonly httpStatus?: number) {
    super(message)
  }
}

const coordinateDepth: Record<string, number> = {
  Point: 0, MultiPoint: 1, LineString: 1, MultiLineString: 2, Polygon: 2, MultiPolygon: 3,
}

function object(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

function invalidPreview(): never {
  throw new PreviewError('PREVIEW_INVALID_GEOJSON', '预览数据不是有效的 WGS84 GeoJSON。')
}

function collectPositions(value: unknown, depth: number, bounds: PreviewBounds) {
  if (!Array.isArray(value)) invalidPreview()
  if (value.length === 0) return
  if (depth > 0) {
    for (const child of value) collectPositions(child, depth - 1, bounds)
    return
  }

  const [longitude, latitude] = value
  if (
    value.length < 2 || value.some((part) => typeof part !== 'number' || !Number.isFinite(part))
    || longitude < -180 || longitude > 180 || latitude < -90 || latitude > 90
  ) invalidPreview()
  bounds[0][0] = Math.min(bounds[0][0], longitude)
  bounds[0][1] = Math.min(bounds[0][1], latitude)
  bounds[1][0] = Math.max(bounds[1][0], longitude)
  bounds[1][1] = Math.max(bounds[1][1], latitude)
}

function collectGeometry(value: unknown, bounds: PreviewBounds) {
  if (value === null) return
  if (!object(value) || typeof value.type !== 'string') invalidPreview()
  if (value.type === 'GeometryCollection') {
    if (!Array.isArray(value.geometries)) invalidPreview()
    for (const geometry of value.geometries) collectGeometry(geometry, bounds)
    return
  }
  if (!Object.hasOwn(coordinateDepth, value.type)) invalidPreview()
  collectPositions(value.coordinates, coordinateDepth[value.type], bounds)
}

function parsePreview(value: unknown): VectorPreview {
  if (!object(value) || value.type !== 'FeatureCollection' || !Array.isArray(value.features)) invalidPreview()
  const bounds: PreviewBounds = [[Infinity, Infinity], [-Infinity, -Infinity]]
  for (const feature of value.features) {
    if (
      !object(feature) || feature.type !== 'Feature'
      || (feature.properties !== null && !object(feature.properties))
    ) invalidPreview()
    collectGeometry(feature.geometry, bounds)
  }
  return {
    data: value as unknown as FeatureCollection,
    bounds: Number.isFinite(bounds[0][0]) ? bounds : null,
  }
}

export function localPreviewUrl(address: string): URL {
  const url = new URL(address, window.location.href)
  if (url.origin !== window.location.origin || !url.pathname.startsWith('/api/')) {
    throw new PreviewError('PREVIEW_ADDRESS_INVALID', '预览地址必须使用本机 API。')
  }
  return url
}

export async function fetchPreviewResponse(url: URL, signal: AbortSignal): Promise<Response> {
  const response = await fetch(url, { signal, redirect: 'error' })
  if (!response.ok) {
    throw new PreviewError('PREVIEW_LOAD_FAILED', `无法加载预览（HTTP ${response.status}）。`, response.status)
  }
  return response
}

export async function fetchVectorPreview(previewUrl: string, signal: AbortSignal): Promise<VectorPreview> {
  const response = await fetchPreviewResponse(localPreviewUrl(previewUrl), signal)
  try {
    return parsePreview(await response.json())
  } catch {
    return invalidPreview()
  }
}
