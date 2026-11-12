import { useEffect, useState } from 'react'
import { ProblemDetails, requestProblem, type Problem } from '../../components/errors/ProblemDetails'
import { MapPanel } from '../map/MapPanel'
import { scriptRunsApi, type ScriptRunStatus, type ScriptRunSummary, type ScriptRunView } from './script-runs-api'
import './script-runs.css'

const statuses: Record<ScriptRunStatus, { label: string; tone: string }> = {
  NOT_STARTED: { label: '已准备', tone: 'neutral' },
  RESULT_PENDING: { label: '结果尚未发布', tone: 'neutral' },
  SUCCEEDED: { label: '运行成功', tone: 'success' },
  FAILED: { label: '运行失败', tone: 'error' },
}

const formatTime = (value: string) => new Date(value).toLocaleString('zh-CN')

function workbenchHref() {
  const url = new URL(window.location.href)
  url.searchParams.delete('view')
  url.searchParams.delete('script_run')
  return `${url.pathname}${url.search}${url.hash}`
}

function ScriptLog({ id, revision }: { id: string; revision: number }) {
  const [expanded, setExpanded] = useState(false)
  const [text, setText] = useState<string | null>(null)
  const [error, setError] = useState<Problem | null>(null)

  useEffect(() => {
    if (!expanded) return
    const controller = new AbortController()
    setText(null)
    setError(null)
    scriptRunsApi.log(id, controller.signal).then((log) => {
      if (!controller.signal.aborted) setText(log)
    }).catch((cause: unknown) => {
      if (!controller.signal.aborted) setError(requestProblem(cause, '日志', '无法读取脚本日志。'))
    })
    return () => controller.abort()
  }, [expanded, id, revision])

  return (
    <section className="script-runs__section" aria-label="脚本日志">
      <h3>进程日志</h3>
      <div className="script-runs__actions">
        <button type="button" aria-expanded={expanded} onClick={() => setExpanded(!expanded)}>
          {expanded ? '收起日志' : '查看日志'}
        </button>
        <a href={scriptRunsApi.logUrl(id)} download={`${id}.log`}>下载日志</a>
      </div>
      {expanded && error && <ProblemDetails problem={error} />}
      {expanded && !error && (text === null
        ? <p role="status" className="workbench__placeholder">正在读取日志…</p>
        : <pre className="script-runs__log">{text || '日志为空'}</pre>)}
    </section>
  )
}

function RunDetail({ id, revision }: { id: string; revision: number }) {
  const [run, setRun] = useState<ScriptRunView | null>(null)
  const [error, setError] = useState<Problem | null>(null)
  const [previewIndex, setPreviewIndex] = useState<number | null>(null)

  useEffect(() => {
    const controller = new AbortController()
    setError(null)
    scriptRunsApi.run(id, controller.signal).then((detail) => {
      if (!controller.signal.aborted) setRun(detail)
    }).catch((cause: unknown) => {
      if (!controller.signal.aborted) setError(requestProblem(cause, '脚本运行', '无法读取脚本运行。'))
    })
    return () => controller.abort()
  }, [id, revision])

  if (error) return <ProblemDetails problem={error} />
  if (!run) return <p role="status" className="workbench__placeholder">正在读取脚本运行…</p>

  const status = statuses[run.status]
  const preview = run.outputs.find((output) => output.index === previewIndex && output.preview_available)
  return (
    <>
      <div className="script-runs__detail-heading">
        <h2>{run.script_id} · v{run.version}</h2>
        <span role="status" className="workbench__status" data-tone={status.tone}>
          {run.timed_out ? '运行超时' : status.label}
        </span>
      </div>
      <dl className="script-runs__metadata">
        <div><dt>运行编号</dt><dd><code>{run.script_run_id}</code></dd></div>
        {run.exit_code !== null && <div><dt>退出码</dt><dd><code>{run.exit_code}</code></dd></div>}
        {run.started_at && <div><dt>开始时间</dt><dd><time dateTime={run.started_at}>{formatTime(run.started_at)}</time></dd></div>}
        {run.finished_at && <div><dt>结束时间</dt><dd><time dateTime={run.finished_at}>{formatTime(run.finished_at)}</time></dd></div>}
      </dl>
      {run.error && <ProblemDetails problem={run.error} />}
      {run.log_available && <ScriptLog id={id} revision={revision} />}

      <section className="script-runs__section" aria-label="输出文件">
        <h3>输出文件</h3>
        {run.outputs.length ? (
          <ul className="script-runs__files" aria-label="脚本输出">
            {run.outputs.map((output) => (
              <li key={output.index}>
                <div><code>{output.path}</code><span>{output.size_bytes.toLocaleString('zh-CN')} 字节</span></div>
                <div className="script-runs__actions">
                  {!output.readable && <span>文件不可读</span>}
                  {output.readable && (
                    <a href={scriptRunsApi.fileUrl(id, output.index)} aria-label={`下载 ${output.path}`} download>下载</a>
                  )}
                  {output.preview_available && (
                    <button type="button" aria-label={`地图预览 ${output.path}`} aria-pressed={previewIndex === output.index}
                      onClick={() => setPreviewIndex(previewIndex === output.index ? null : output.index)}>地图预览</button>
                  )}
                </div>
              </li>
            ))}
          </ul>
        ) : <p className="workbench__placeholder">暂无输出文件</p>}
        {preview && (
          <div className="script-runs__preview">
            <MapPanel layers={[{
              id: `${id}:${preview.index}`, name: preview.path, previewUrl: scriptRunsApi.previewUrl(id, preview.index), kind: 'asset',
            }]} />
          </div>
        )}
      </section>
    </>
  )
}

export function ScriptRunsPage() {
  const [runs, setRuns] = useState<ScriptRunSummary[] | null>(null)
  const [selected, setSelected] = useState(() => new URLSearchParams(window.location.search).get('script_run'))
  const [error, setError] = useState<Problem | null>(null)
  const [revision, setRevision] = useState(0)

  useEffect(() => {
    const controller = new AbortController()
    setError(null)
    scriptRunsApi.list(controller.signal).then((catalog) => {
      if (controller.signal.aborted) return
      setRuns(catalog.runs)
      setSelected((current) => current ?? catalog.runs[0]?.script_run_id ?? null)
    }).catch((cause: unknown) => {
      if (!controller.signal.aborted) setError(requestProblem(cause, '脚本运行', '无法读取脚本运行目录。'))
    })
    return () => controller.abort()
  }, [revision])

  function select(id: string) {
    setSelected(id)
    const url = new URL(window.location.href)
    url.searchParams.set('script_run', id)
    window.history.replaceState(null, '', url)
  }

  return (
    <main className="script-runs">
      <header className="script-runs__header workbench__header">
        <h1>本地脚本运行</h1>
        <div className="script-runs__actions">
          <a href={workbenchHref()}>返回工作台</a>
          <button type="button" onClick={() => setRevision((previous) => previous + 1)}>刷新脚本运行</button>
        </div>
      </header>
      <div className="script-runs__content">
        <aside className="script-runs__panel" aria-label="脚本运行目录">
          <h2>运行记录</h2>
          {error && <ProblemDetails problem={error} />}
          {!error && runs === null && <p role="status" className="workbench__placeholder">正在读取运行记录…</p>}
          {runs?.length === 0 && <p className="workbench__placeholder">暂无已发布的脚本快照</p>}
          {runs && runs.length > 0 && (
            <ul className="script-runs__catalog">
              {runs.map((run) => (
                <li key={run.script_run_id}>
                  <button type="button" title={run.script_run_id}
                    aria-label={`${run.script_id} · v${run.version} · ${statuses[run.status].label} · ${run.script_run_id}`}
                    aria-current={selected === run.script_run_id ? 'true' : undefined}
                    onClick={() => select(run.script_run_id)}>
                    <strong>{run.script_id} · v{run.version}</strong>
                    <span className="workbench__status" data-tone={statuses[run.status].tone}>{statuses[run.status].label}</span>
                    <time dateTime={run.created_at}>{formatTime(run.created_at)}</time>
                  </button>
                </li>
              ))}
            </ul>
          )}
        </aside>
        <section className="script-runs__panel" aria-label="脚本运行详情">
          {selected ? <RunDetail key={selected} id={selected} revision={revision} />
            : <p className="workbench__placeholder">请选择脚本运行</p>}
        </section>
      </div>
    </main>
  )
}
