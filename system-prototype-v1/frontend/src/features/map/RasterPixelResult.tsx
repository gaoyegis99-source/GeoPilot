import type { RasterPixelValue, RasterValueQuery } from '../../api/workbench'
import { ProblemDetails, type Problem } from '../../components/errors/ProblemDetails'

export type PixelLookup = { artifactId: string; artifactName: string } & (
  | { phase: 'loading' }
  | { phase: 'error'; problem: Problem }
  | { phase: 'ready'; result: RasterPixelValue }
)

export function matchesPixelQuery(result: RasterPixelValue, artifactId: string, query: RasterValueQuery): boolean {
  if (
    !result || result.artifact_id !== artifactId || result.source_fingerprint !== query.source_fingerprint
    || result.longitude !== query.longitude || result.latitude !== query.latitude || result.band !== query.band
    || typeof result.source_crs !== 'string' || !result.source_crs || typeof result.dtype !== 'string'
    || ![result.source_x, result.source_y, result.scale, result.offset].every(Number.isFinite)
    || (result.unit !== null && typeof result.unit !== 'string')
  ) return false
  if (result.status === 'outside') {
    return result.row === null && result.column === null && result.value === null && result.nodata === false
  }
  if (!Number.isSafeInteger(result.row) || !Number.isSafeInteger(result.column) || result.row < 0 || result.column < 0) return false
  if (result.status === 'nodata') return result.nodata === true && result.value === null
  return result.status === 'value' && result.nodata === false && (
    Number.isFinite(result.value) || (typeof result.value === 'string' && /^-?\d+$/.test(result.value))
  )
}

function displayValue(result: RasterPixelValue): string {
  if (result.status === 'outside') return '范围外'
  if (result.status === 'nodata') return '无数据（NoData）'
  if (result.unit === 'degree' || result.unit === '°') return `${result.value}°`
  if (result.unit === 'percent' || result.unit === '%') return `${result.value}%`
  if (result.unit === 'class') return `${result.value} · 类别`
  return result.unit ? `${result.value} ${result.unit}` : String(result.value)
}

export function RasterPixelResult({ lookup }: { lookup: PixelLookup }) {
  return (
    <section className="map-panel__pixel" aria-label="原始像元" data-artifact-id={lookup.artifactId}>
      <h3>原始像元</h3>
      <p>{lookup.artifactName}</p>
      {lookup.phase === 'loading' && <p role="status">正在读取原始像元…</p>}
      {lookup.phase === 'error' && <ProblemDetails problem={lookup.problem} />}
      {lookup.phase === 'ready' && (
        <dl aria-live="polite">
          <dt>原始值</dt><dd>{displayValue(lookup.result)}</dd>
          {lookup.result.status !== 'outside' && <>
            <dt>行</dt><dd>{lookup.result.row}</dd>
            <dt>列</dt><dd>{lookup.result.column}</dd>
          </>}
          <dt>源坐标</dt><dd>{lookup.result.source_x}, {lookup.result.source_y} · {lookup.result.source_crs}</dd>
        </dl>
      )}
    </section>
  )
}
