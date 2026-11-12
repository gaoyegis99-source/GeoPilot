import { useEffect, useRef, useState, type ReactNode } from 'react'
import { ApiError, type RunStatus, type RunStep, workbenchApi } from '../../api/workbench'
import { ProblemDetails, type ProblemActions } from '../../components/errors/ProblemDetails'
import './runs.css'

const stepLabels: Record<RunStep['status'], string> = {
  NOT_STARTED: '待执行', RUNNING: '运行中', SUCCEEDED: '已完成', FAILED: '失败', CANCELLED: '已取消',
}

export function isRunActive(status: RunStatus | null) {
  return status !== null && (
    status.execution_status === 'NOT_STARTED' || status.execution_status === 'RUNNING' || status.check_status === 'CHECKING'
  )
}

function elapsed(step?: RunStep) {
  if (!step?.started_at) return null
  const start = Date.parse(step.started_at)
  const finish = step.finished_at ? Date.parse(step.finished_at) : Date.now()
  return Number.isFinite(start) && Number.isFinite(finish) ? Math.max(0, Math.floor((finish - start) / 1000)) : null
}

interface RunPanelProps {
  runId: string | null
  onStatus?: (status: RunStatus) => void
  actions?: ProblemActions
  refreshKey?: number
  pendingReportId?: string
  pollMilliseconds?: number
  children?: ReactNode
}

export function RunPanel({
  runId, onStatus, actions, refreshKey = 0, pendingReportId, pollMilliseconds = 1000, children,
}: RunPanelProps) {
  const [status, setStatus] = useState<RunStatus | null>(null)
  const [steps, setSteps] = useState<RunStep[]>([])
  const [requestError, setRequestError] = useState<string | null>(null)
  const [cancelPending, setCancelPending] = useState(false)
  const [log, setLog] = useState<{ step: string; text: string } | null>(null)
  const [logLoading, setLogLoading] = useState(false)
  const activeId = useRef(runId)
  const latest = useRef<RunStatus | null>(null)
  const callback = useRef(onStatus)
  activeId.current = runId
  callback.current = onStatus

  useEffect(() => {
    latest.current = null
    setStatus(null)
    setSteps([])
    setLog(null)
    setRequestError(null)
    setCancelPending(false)
    setLogLoading(false)
    if (!runId) return

    const abort = new AbortController()
    let timer: ReturnType<typeof setTimeout> | undefined

    async function poll(id: string) {
      try {
        const next = await workbenchApi.run(id, abort.signal)
        if (abort.signal.aborted) return
        const previous = latest.current
        const claimedRecheck = pendingReportId && previous?.check_status === 'NOT_CHECKED' && next.check_status === 'CHECKING'
        const stale = previous && isRunActive(next) && !claimedRecheck
          && (!isRunActive(previous) || previous.cancel_requested && !next.cancel_requested)
        if (!stale) {
          latest.current = next
          setStatus(next)
          callback.current?.(next)
        }
        setRequestError(null)
        const result = await workbenchApi.steps(id, abort.signal)
        if (!abort.signal.aborted) setSteps(result.steps)
      } catch (cause) {
        if (!abort.signal.aborted) {
          setRequestError(cause instanceof ApiError ? cause.message : '无法读取本机运行进度。')
        }
      } finally {
        const current = latest.current
        const waitingForReport = pendingReportId && current?.current_report_id !== pendingReportId
          && current?.error?.code !== 'STORAGE_FAILED'
        if (!abort.signal.aborted && (current === null || isRunActive(current) || waitingForReport)) {
          timer = setTimeout(() => { void poll(id) }, pollMilliseconds)
        }
      }
    }

    void poll(runId)
    return () => {
      abort.abort()
      if (timer !== undefined) clearTimeout(timer)
    }
  }, [runId, refreshKey, pendingReportId, pollMilliseconds])

  async function cancel() {
    if (!runId) return
    setCancelPending(true)
    setRequestError(null)
    try {
      const next = await workbenchApi.cancel(runId)
      if (activeId.current !== next.run_id) return
      if (latest.current && !isRunActive(latest.current) && isRunActive(next)) return
      latest.current = next
      setStatus(next)
      callback.current?.(next)
    } catch (cause) {
      if (activeId.current === runId) setRequestError(cause instanceof ApiError ? cause.message : '无法提交取消请求。')
    } finally {
      if (activeId.current === runId) setCancelPending(false)
    }
  }

  async function openLog(id: string, step: string) {
    setLogLoading(true)
    setRequestError(null)
    try {
      const text = await workbenchApi.log(id, step, 60)
      if (activeId.current === id) setLog({ step, text })
    } catch (cause) {
      if (activeId.current === id) setRequestError(cause instanceof ApiError ? cause.message : '无法读取步骤日志。')
    } finally {
      if (activeId.current === id) setLogLoading(false)
    }
  }

  const step = steps.find((entry) => entry.step_key === status?.current_step)
  const duration = elapsed(step)
  const active = isRunActive(status)

  return (
    <div className="run-panel">
      {children}
      {runId && (
        <>
          {status === null && !requestError && <p role="status">正在读取运行…</p>}
          {status?.current_step && (
            <p>当前步骤 <code>{status.current_step}</code>{duration !== null && <> · {duration} 秒</>}</p>
          )}
          {active && (
            <button type="button" disabled={cancelPending || status?.cancel_requested} onClick={cancel}>
              {status?.cancel_requested ? '已请求取消' : cancelPending ? '正在提交…' : '取消运行'}
            </button>
          )}
          {requestError && <p role="alert" className="run-panel__error">{requestError}</p>}
          {status?.error && (
            <ProblemDetails
              problem={status.error} actions={actions}
              logHref={status.error.log_path ? workbenchApi.logUrl(runId, status.error.step_key) : undefined}
            />
          )}
          {steps.length > 0 && (
            <ol className="run-panel__steps" aria-label="运行步骤">
              {steps.map((entry) => (
                <li key={entry.step_key}>
                  <span><code>{entry.step_key}</code> · {stepLabels[entry.status]}</span>
                  {entry.log_available && (
                    <button type="button" disabled={logLoading} onClick={() => { void openLog(runId, entry.step_key) }}>查看日志</button>
                  )}
                </li>
              ))}
            </ol>
          )}
          {log && (
            <div className="run-panel__log">
              <p><code>{log.step}</code> <a href={workbenchApi.logUrl(runId, log.step)} target="_blank" rel="noreferrer">查看完整日志</a></p>
              <pre>{log.text || '日志为空。'}</pre>
            </div>
          )}
        </>
      )}
    </div>
  )
}
