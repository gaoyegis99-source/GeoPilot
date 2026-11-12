import { useState } from 'react'
import { ApiError, type ArtifactEntry, type ArtifactTable, type Json, type RunArtifacts, workbenchApi } from '../../api/workbench'
import './artifacts.css'

const roleLabels: Record<string, string> = {
  filtered_features: '筛选成果', road_buffer: '道路缓冲区', buffer_extent: '道路缓冲区', selected_targets: '目标对象',
  clipped_targets: '裁剪成果', original_targets: '裁前目标',
  target_zone_details: '目标归属明细', counts_by_zone: '分区计数', zone_statistics: '分区统计',
  allocation_details: '分配明细', change_polygons: '变化图斑', transition_matrix: '转移矩阵',
  terrain_raster: '地形栅格', threshold_regions: '阈值区域', area_statistics: '面积统计',
  first_ndvi: '第一期 NDVI', second_ndvi: '第二期 NDVI', change_raster: 'NDVI 差值', change_classes: 'NDVI 变化类别',
  first_quality_mask: '第一期质量掩膜', second_quality_mask: '第二期质量掩膜',
  sensor_configuration: '传感器配置', calibration_report: '反射率定标', alignment_report: '两期对齐记录',
  route_edges: '最短路径路段', route_summary: '最短路径摘要',
}

export const artifactTitle = (role: string) => roleLabels[role] ?? role

type Statistics = { id: string; table: ArtifactTable } | { id: string; document: Json }

export function ArtifactsPanel({ record, loading, error, selectedArtifactId, onLocate }: {
  record: RunArtifacts | null
  loading?: boolean
  error?: string | null
  selectedArtifactId?: string
  onLocate?: (id: string) => void
}) {
  const [statistics, setStatistics] = useState<Statistics | null>(null)
  const [pending, setPending] = useState<string | null>(null)
  const [statError, setStatError] = useState<{ id: string; message: string } | null>(null)

  async function openStatistics(artifact: ArtifactEntry) {
    if (statistics?.id === artifact.id) {
      setStatistics(null)
      return
    }
    setPending(artifact.id)
    setStatError(null)
    try {
      const data = artifact.kind === 'json'
        ? { id: artifact.id, document: await workbenchApi.artifactDocument(artifact.id) }
        : { id: artifact.id, table: await workbenchApi.artifactTable(artifact.id) }
      setStatistics(data)
    } catch (cause) {
      setStatError({ id: artifact.id, message: cause instanceof ApiError ? cause.message : '无法读取统计成果。' })
    } finally {
      setPending(null)
    }
  }

  return (
    <div className="artifacts-panel">
      {loading && <p role="status">正在读取成果…</p>}
      {error && <p role="alert" className="artifacts-panel__error">{error}</p>}
      {!loading && !error && !record?.artifacts.length && <p className="workbench__placeholder">暂无正式成果</p>}
      {record && record.artifacts.length > 0 && <>
        <p>成果所属运行 <code title={record.run_id}>{record.run_id.slice(0, 8)}</code></p>
        <ul className="artifacts-panel__list" aria-label="正式成果">
          {record.artifacts.map((artifact) => {
            const canReadStatistics = artifact.kind === 'json' || (artifact.kind === 'table' && artifact.format.toUpperCase() === 'CSV')
            const count = artifact.metadata.feature_count ?? artifact.metadata.row_count
            return (
              <li
                className="artifacts-panel__item" key={artifact.id}
                data-selected={selectedArtifactId === artifact.id} aria-current={selectedArtifactId === artifact.id || undefined}
              >
                <div className="artifacts-panel__title">
                  <strong>{artifactTitle(artifact.role)}</strong><span>{artifact.format}</span>
                </div>
                <p className={artifact.readable ? 'workbench__placeholder' : 'artifacts-panel__error'}>
                  {artifact.readable ? '可读取' : '文件不可读'}
                  {typeof count === 'number' ? ` · ${count} 条` : ''}
                </p>
                <div className="artifacts-panel__actions">
                  {artifact.readable && <a href={workbenchApi.artifactFileUrl(artifact.id)} download>下载原文件</a>}
                  {onLocate && (artifact.kind === 'vector' || artifact.kind === 'raster') && artifact.readable && (
                    <button type="button" onClick={() => onLocate(artifact.id)}>地图定位</button>
                  )}
                  {canReadStatistics && artifact.readable && (
                    <button type="button" disabled={pending !== null} onClick={() => { void openStatistics(artifact) }}>
                      {pending === artifact.id ? '正在读取…' : statistics?.id === artifact.id ? '收起统计' : '查看统计'}
                    </button>
                  )}
                </div>
                {statError?.id === artifact.id && <p role="alert" className="artifacts-panel__error">{statError.message}</p>}
                {statistics?.id === artifact.id && (
                  'table' in statistics ? <StatisticsTable table={statistics.table} /> :
                    <pre className="artifacts-panel__json">{JSON.stringify(statistics.document, null, 2)}</pre>
                )}
                <details>
                  <summary>来源详情</summary>
                  <dl>
                    <dt>成果编号</dt><dd>{artifact.id}</dd>
                    <dt>运行编号</dt><dd>{artifact.run_id}</dd>
                    <dt>方案</dt><dd>{artifact.source.plan_id} · v{artifact.source.plan_version}</dd>
                    <dt>步骤</dt><dd>{artifact.source.step_key}</dd>
                    <dt>工具</dt><dd>{artifact.source.tool.id} · v{artifact.source.tool.version}</dd>
                    {Object.entries(artifact.source.inputs).map(([role, asset]) => (
                      <div key={role}>
                        <dt>{role}</dt><dd>{asset.asset_id} · v{asset.asset_version}</dd>
                      </div>
                    ))}
                  </dl>
                </details>
              </li>
            )
          })}
        </ul>
      </>}
      {record && record.diagnostics.length > 0 && (
        <details className="artifacts-panel__diagnostics">
          <summary>排错文件</summary>
          <ul>
            {record.diagnostics.map((file) => (
              <li key={`${file.step_key}:${file.path}`}>
                <code>{file.path}</code> {!file.readable && <span>不可读</span>}
                {file.type === 'log' && file.readable && (
                  <> <a href={workbenchApi.logUrl(record.run_id, file.step_key)} target="_blank" rel="noreferrer">打开日志</a></>
                )}
              </li>
            ))}
          </ul>
        </details>
      )}
    </div>
  )
}

function StatisticsTable({ table }: { table: ArtifactTable }) {
  return (
    <div className="artifacts-panel__table">
      <table>
        <thead><tr>{table.columns.map((column, index) => <th scope="col" key={index}>{column}</th>)}</tr></thead>
        <tbody>
          {table.rows.map((row, index) => <tr key={index}>{row.map((cell, column) => <td key={column}>{cell}</td>)}</tr>)}
        </tbody>
      </table>
      {table.truncated && <p>更多记录见原文件。</p>}
    </div>
  )
}
