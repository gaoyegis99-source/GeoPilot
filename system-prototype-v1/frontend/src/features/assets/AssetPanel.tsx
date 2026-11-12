import { useId, useState, type FormEvent } from 'react'
import { ApiError, type AssetEntry, type AssetRef, type LayerEntry } from '../../api/workbench'
import './assets.css'

const roleLabels: Record<string, string> = {
  roads: '道路', targets: '目标对象', zones: '统计分区', features: '筛选要素',
  analysis_extent: '分析范围', relation_area: '关系范围', first_classes: '第一期地类', second_classes: '第二期地类',
}

export function assetKey(asset: AssetRef) {
  return `${asset.asset_id}@${asset.asset_version}`
}

function assetName(asset: AssetEntry) {
  return asset.source.layer ?? asset.source.path.split(/[\\/]/).at(-1) ?? asset.asset_id
}

interface AssetPanelProps {
  assets: AssetEntry[]
  visible: AssetRef[]
  assigned: Readonly<Partial<Record<string, AssetRef>>>
  loading?: boolean
  error?: string | null
  onToggle: (asset: AssetEntry) => void
  onRegister: (path: string, layer?: string) => Promise<AssetEntry>
  onListLayers: (path: string) => Promise<LayerEntry[]>
}

export function AssetPanel({ assets, visible, assigned, loading, error, onToggle, onRegister, onListLayers }: AssetPanelProps) {
  const pathId = useId()
  const layerId = useId()
  const formId = useId()
  const [open, setOpen] = useState(false)
  const [path, setPath] = useState('')
  const [layer, setLayer] = useState('')
  const [layers, setLayers] = useState<LayerEntry[] | null>(null)
  const [pending, setPending] = useState<'layers' | 'register' | null>(null)
  const [failure, setFailure] = useState<{ message: string; field?: string } | null>(null)
  const geopackage = path.trim().toLowerCase().endsWith('.gpkg')
  const visibleKeys = new Set(visible.map(assetKey))

  function showFailure(cause: unknown) {
    if (cause instanceof ApiError) {
      const detail = cause.detail
      const field = typeof detail === 'object' && detail !== null && 'field' in detail && typeof detail.field === 'string'
        ? detail.field : undefined
      setFailure({ message: cause.message, field })
    } else {
      setFailure({ message: '无法连接本机服务。' })
    }
  }

  async function inspectLayers() {
    setPending('layers')
    setFailure(null)
    try {
      const found = await onListLayers(path.trim())
      setLayers(found)
      setLayer(found.length === 1 ? found[0].name : '')
    } catch (cause) {
      showFailure(cause)
    } finally {
      setPending(null)
    }
  }

  async function register(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    if (!path.trim() || pending) return
    setPending('register')
    setFailure(null)
    try {
      await onRegister(path.trim(), geopackage ? layer.trim() || undefined : undefined)
      setOpen(false)
      setPath('')
      setLayer('')
      setLayers(null)
    } catch (cause) {
      showFailure(cause)
    } finally {
      setPending(null)
    }
  }

  return (
    <div className="asset-panel">
      {error && <p role="alert" className="asset-panel__error">{error}</p>}
      {loading && <p className="workbench__placeholder" role="status">正在读取资产…</p>}
      {!loading && !error && assets.length === 0 && <p className="workbench__placeholder">暂无数据</p>}
      {assets.length > 0 && (
        <ul className="asset-panel__list" aria-label="已登记资产">
          {assets.map((asset) => {
            const name = assetName(asset)
            const assignedRoles = Object.entries(assigned).filter(([, ref]) => ref && assetKey(ref) === assetKey(asset))
            const available = asset.availability === 'AVAILABLE'
            return (
              <li className="asset-panel__item" key={assetKey(asset)}>
                <label className="asset-panel__row">
                  <input
                    type="checkbox"
                    checked={visibleKeys.has(assetKey(asset))}
                    disabled={!available || asset.metadata.kind !== 'vector'}
                    aria-label={`显示 ${name} v${asset.asset_version}`}
                    onChange={() => onToggle(asset)}
                  />
                  <span className="asset-panel__name">{name}</span>
                  <span className="asset-panel__version">v{asset.asset_version}</span>
                </label>
                <p className="asset-panel__meta">
                  {asset.metadata.format}
                  {asset.metadata.geometry_type ? ` · ${asset.metadata.geometry_type}` : ''}
                  {asset.metadata.feature_count != null ? ` · ${asset.metadata.feature_count} 个要素` : ''}
                  {!available ? ' · 不可用' : ''}
                </p>
                {assignedRoles.length > 0 && (
                  <ul className="asset-panel__roles" aria-label="任务角色">
                    {assignedRoles.map(([role]) => <li key={role}>{roleLabels[role] ?? role}</li>)}
                  </ul>
                )}
                <details className="asset-panel__details">
                  <summary>资产详情</summary>
                  <dl>
                    <dt>资产编号</dt><dd>{asset.asset_id}</dd>
                    <dt>坐标系</dt><dd>{asset.metadata.crs ?? '未声明'}</dd>
                    <dt>来源</dt><dd>{asset.source.path}</dd>
                  </dl>
                  {asset.metadata.issues?.length ? <ul>{asset.metadata.issues.map((issue) => <li key={issue}>{issue}</li>)}</ul> : null}
                </details>
              </li>
            )
          })}
        </ul>
      )}

      <div className="asset-panel__import">
        <button type="button" className="workbench__action" aria-expanded={open} aria-controls={formId} onClick={() => setOpen(!open)}>
          {open ? '收起导入' : '导入数据'}
        </button>
        {open && (
          <form id={formId} className="asset-panel__form" onSubmit={register}>
            <label htmlFor={pathId}>本机文件路径</label>
            <input
              id={pathId} value={path} disabled={pending !== null} required
              onChange={(event) => { setPath(event.target.value); setLayer(''); setLayers(null); setFailure(null) }}
            />
            {geopackage && (
              <>
                <button type="button" className="asset-panel__secondary" onClick={inspectLayers} disabled={pending !== null}>
                  {pending === 'layers' ? '正在读取…' : '读取图层'}
                </button>
                <label htmlFor={layerId}>GeoPackage 图层</label>
                {layers ? (
                  <select id={layerId} value={layer} disabled={pending !== null} onChange={(event) => setLayer(event.target.value)}>
                    <option value="">选择图层</option>
                    {layers.map((item) => <option key={item.name} value={item.name}>{item.name}</option>)}
                  </select>
                ) : (
                  <input id={layerId} value={layer} disabled={pending !== null} onChange={(event) => setLayer(event.target.value)} />
                )}
              </>
            )}
            {failure && (
              <p role="alert" className="asset-panel__error">
                <span>{failure.message}</span>{failure.field && <> <code>{failure.field}</code></>}
              </p>
            )}
            <button type="submit" className="workbench__action" disabled={pending !== null}>
              {pending === 'register' ? '正在登记…' : '登记资产'}
            </button>
          </form>
        )}
      </div>
    </div>
  )
}
