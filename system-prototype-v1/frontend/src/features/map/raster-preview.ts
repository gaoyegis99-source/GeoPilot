import type { ImageSourceSpecification } from 'maplibre-gl'
import { fetchPreviewResponse, localPreviewUrl, PreviewError, type PreviewBounds } from './preview'

export const rasterPalettes = { terrain: '地形配色', grayscale: '灰度' } as const
export type RasterPalette = keyof typeof rasterPalettes
type Position = [number, number]

export interface RasterPreview {
  coordinates: ImageSourceSpecification['coordinates']
  bounds: PreviewBounds
  width: number
  height: number
  palette: RasterPalette
  sourceCrs: string
  sourceResolution: Position
  displayResolution: Position
  png: Blob
}

const maximumPreviewSize = 1024

function numberPair(value: unknown): value is Position {
  return Array.isArray(value) && value.length === 2 && value.every((part) => typeof part === 'number' && Number.isFinite(part))
}

function positiveDimension(value: unknown): value is number {
  return typeof value === 'number' && Number.isInteger(value) && value >= 1 && value <= maximumPreviewSize
}

function invalidRaster(): never {
  throw new PreviewError('PREVIEW_INVALID_RASTER', '栅格预览与登记成果不符，或预览图像无法读取。')
}

function parseCoordinates(value: unknown): { coordinates: RasterPreview['coordinates']; bounds: PreviewBounds } {
  if (!Array.isArray(value) || value.length !== 4 || !value.every((position) => (
    numberPair(position) && Math.abs(position[0]) <= 180 && Math.abs(position[1]) <= 90
  ))) invalidRaster()
  const coordinates = value as RasterPreview['coordinates']
  const [topLeft, topRight, bottomRight, bottomLeft] = coordinates
  if (topLeft[0] >= topRight[0] || bottomLeft[0] >= bottomRight[0] || topLeft[1] <= bottomLeft[1] || topRight[1] <= bottomRight[1]) {
    invalidRaster()
  }
  return {
    coordinates,
    bounds: [
      [Math.min(topLeft[0], bottomLeft[0]), Math.min(bottomLeft[1], bottomRight[1])],
      [Math.max(topRight[0], bottomRight[0]), Math.max(topLeft[1], topRight[1])],
    ],
  }
}

export async function fetchRasterPreview(
  previewUrl: string, artifactId: string, sourceFingerprint: string, palette: RasterPalette, signal: AbortSignal,
): Promise<RasterPreview> {
  const url = localPreviewUrl(previewUrl)
  url.searchParams.set('palette', palette)
  const response = await fetchPreviewResponse(url, signal)
  const value: unknown = await response.json().catch(invalidRaster)
  if (typeof value !== 'object' || value === null || Array.isArray(value)) invalidRaster()
  const data = value as Record<string, unknown>
  if (
    data.artifact_id !== artifactId || data.source_fingerprint !== sourceFingerprint || data.palette !== palette
    || typeof data.image_url !== 'string' || data.band !== 1 || data.display_crs !== 'EPSG:3857'
    || typeof data.source_crs !== 'string' || !data.source_crs || !positiveDimension(data.width) || !positiveDimension(data.height)
    || !numberPair(data.source_resolution) || !data.source_resolution.every((part) => part > 0)
    || !numberPair(data.display_resolution) || !data.display_resolution.every((part) => part > 0)
  ) invalidRaster()

  const placement = parseCoordinates(data.coordinates)
  const imageUrl = localPreviewUrl(data.image_url)
  const imageParameters = { palette, band: '1', max_size: String(maximumPreviewSize), source_fingerprint: sourceFingerprint }
  if (imageUrl.pathname !== `${url.pathname}.png` || !Object.entries(imageParameters).every(([name, expected]) => {
    const values = imageUrl.searchParams.getAll(name)
    return values.length === 1 && values[0] === expected
  })) invalidRaster()
  signal.throwIfAborted()
  const image = await fetchPreviewResponse(imageUrl, signal)
  if (image.headers.get('content-type')?.split(';')[0] !== 'image/png') invalidRaster()
  return {
    ...placement, palette, width: data.width, height: data.height, sourceCrs: data.source_crs,
    sourceResolution: data.source_resolution, displayResolution: data.display_resolution, png: await image.blob(),
  }
}
