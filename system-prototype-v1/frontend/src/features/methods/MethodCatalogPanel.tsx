import { useId } from 'react'
import type { MethodEntry, MethodRef } from '../../api/workbench'
import './method-catalog.css'

export interface MethodCatalogPanelProps {
  methods: readonly MethodEntry[]
  selected: MethodRef | null
  onSelect: (method: MethodRef) => void
  loading?: boolean
  disabled?: boolean
  error?: string | null
}

const inputTypeLabels: Record<string, string> = {
  vector: '矢量',
  vector_point: '矢量点',
  vector_line: '矢量线',
  vector_polygon: '矢量面',
  vector_point_or_polygon: '矢量点或面',
  raster: '栅格',
  raster_elevation: '高程栅格',
  raster_product: '原 DN 遥感产品',
  table: '表格',
}

const issueLabels: Record<string, string> = {
  TOOL_UNAVAILABLE: '执行工具不可用',
  TOOL_NOT_REGISTERED: '所需工具未登记',
  MISSING_TOOL_BINDING: '缺少工具绑定',
  UNKNOWN_CHECK_RULE: '检查规则未登记',
}

function MethodCatalogItem({
  entry, selected, onSelect, disabled,
}: { entry: MethodEntry; selected: MethodRef | null; onSelect: (method: MethodRef) => void; disabled?: boolean }) {
  const { title, description, definition, available, issues } = entry
  const { id, version, inputs } = definition
  const isSelected = selected?.id === id && selected.version === version
  const status = available ? (isSelected ? '已选择' : '可用') : '不可用'
  const roles = Object.entries(inputs)

  return (
    <li>
      <details className="method-catalog__entry">
        <summary aria-current={isSelected || undefined}>
          <span className="method-catalog__name">{title}</span>
          <span className="method-catalog__status" data-available={available}>{status}</span>
          <span className="method-catalog__identity">
            <code>{id}</code> <span>版本 {version}</span>
          </span>
        </summary>
        <div className="method-catalog__details">
          <p>{description}</p>
          {roles.length > 0 && (
            <>
              <h4>输入角色</h4>
              <dl className="method-catalog__roles">
                {roles.map(([role, type]) => (
                  <div key={role}>
                    <dt><code>{role}</code></dt>
                    <dd>{inputTypeLabels[type] ?? type}</dd>
                  </div>
                ))}
              </dl>
            </>
          )}
          {issues.length > 0 && (
            <ul className="method-catalog__issues">
              {issues.map(issue => (
                <li key={JSON.stringify([issue.code, issue.field, issue.message])}>
                  <strong>{issueLabels[issue.code] ?? '方法配置问题'}</strong>
                  <p>{issue.message}</p>
                  {issue.field && <code>{issue.field}</code>}
                </li>
              ))}
            </ul>
          )}
          {!isSelected && (
            <button type="button" disabled={!available || disabled} onClick={() => onSelect({ id, version })}>
              选择方法
            </button>
          )}
        </div>
      </details>
    </li>
  )
}

export function MethodCatalogPanel({ methods, selected, onSelect, loading, disabled, error }: MethodCatalogPanelProps) {
  const titleId = useId()

  return (
    <section className="method-catalog" aria-labelledby={titleId}>
      <h3 id={titleId}>方法目录</h3>
      {loading && <p role="status">正在读取方法目录…</p>}
      {error && <p role="alert">{error}</p>}
      {methods.length === 0 && !loading && !error && <p>暂无已登记的方法</p>}
      {methods.length > 0 && (
        <ul className="method-catalog__entries" role="list">
          {methods.map(entry => (
            <MethodCatalogItem
              key={`${entry.definition.id}@${entry.definition.version}`} entry={entry} selected={selected} onSelect={onSelect} disabled={disabled}
            />
          ))}
        </ul>
      )}
    </section>
  )
}
