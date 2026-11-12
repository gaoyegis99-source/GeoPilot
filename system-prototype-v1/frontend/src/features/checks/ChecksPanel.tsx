import { useEffect, useState, type ComponentProps } from 'react'
import { ApiError, type CheckFinding, type CheckReport, type CheckStatus, type Json, type RunChecks, workbenchApi } from '../../api/workbench'
import { ProblemDetails } from '../../components/errors/ProblemDetails'
import './checks-panel.css'

export interface ChecksPanelProps {
  runId: string
  checkStatus: CheckStatus | undefined
  currentReportId?: string | null
  onRecheck?: () => void
  recheckPending?: boolean
}

const statusLabels: Record<CheckStatus, string> = {
  NOT_CHECKED: '未完成', CHECKING: '检查中', PASSED: '通过', WARNING: '有警告', FAILED: '未通过',
}

const phaseLabels = {
  input: '输入检查', process: '过程检查', artifact: '成果检查',
} satisfies Record<CheckFinding['phase'], string>

interface RuleDetails {
  name: string
  severity: string
  passed: boolean | null
  message: string
  observed?: Json
  expected?: Json
  step?: string | null
  field?: string
  version?: number
}

type Problem = ComponentProps<typeof ProblemDetails>['problem']

function Value({ value }: { value?: Json }) {
  if (value === undefined) return <span>未记录</span>
  if (typeof value === 'object' && value !== null) return <pre>{JSON.stringify(value, null, 2)}</pre>
  return <code>{value === null ? 'null' : String(value)}</code>
}

function Rule({ details }: { details: RuleDetails }) {
  const verdict = details.passed === true ? '通过' : details.passed === false ? '未通过' : '未执行'
  const severity = details.severity === 'blocking' ? '阻断' : details.severity === 'warning' ? '警告' : details.severity
  const status = details.passed === null ? 'NOT_CHECKED'
    : details.passed ? 'PASSED' : details.severity === 'warning' ? 'WARNING' : 'FAILED'
  return (
    <li className="checks-panel__rule">
      <details open={details.passed === false || undefined}>
        <summary>
          <code>{details.name}</code>
          <span className="checks-panel__severity">{severity}</span>
          <strong className="checks-panel__status" data-status={status}>{verdict}</strong>
        </summary>
        <div className="checks-panel__rule-detail">
          <p>{details.message}</p>
          <dl>
            {details.version !== undefined && <><dt>规则版本</dt><dd><code>{details.version}</code></dd></>}
            {details.step && <><dt>步骤</dt><dd><code>{details.step}</code></dd></>}
            {details.field && <><dt>字段</dt><dd><code>{details.field}</code></dd></>}
            <dt>观察值</dt><dd><Value value={details.observed} /></dd>
            <dt>期望值</dt><dd><Value value={details.expected} /></dd>
          </dl>
        </div>
      </details>
    </li>
  )
}

function ReportDetails({ report, historical }: { report: CheckReport; historical: boolean }) {
  const details = report.report
  const methodRules = Object.entries(details.rules ?? {})
  const phaseFindings = details.phase_findings ?? {}
  const when = new Date(report.created_at)
  const displayTime = Number.isFinite(when.getTime()) ? when.toLocaleString('zh-CN') : report.created_at
  return (
    <article className="checks-panel__report" aria-label="检查报告明细">
      <header className="checks-panel__report-header">
        <div>
          <span>{historical ? '历史报告' : '当前报告'}</span>
          <strong className="checks-panel__status" data-status={report.status} aria-label="报告状态">
            {statusLabels[report.status]}
          </strong>
        </div>
        <time dateTime={report.created_at}>{displayTime}</time>
      </header>
      <details className="checks-panel__report-id">
        <summary>报告编号</summary>
        <code>{report.id}</code>
      </details>
      {details.interruption && <ProblemDetails problem={{ ...details.interruption, phase: 'check' }} />}
      {details.pending_rules?.length > 0 && (
        <p className="checks-panel__pending">
          待检查规则 {details.pending_rules.map((name) => <code key={name}>{name}</code>)}
        </p>
      )}
      {methodRules.length > 0 && (
        <section>
          <h3>方法规则</h3>
          <ul className="checks-panel__rules">
            {methodRules.map(([name, rule]) => (
              <Rule key={name} details={{ ...rule, name, version: report.rule_versions[name] }} />
            ))}
          </ul>
        </section>
      )}
      {(Object.entries(phaseLabels) as [CheckFinding['phase'], string][]).map(([phase, label]) => {
        const findings = phaseFindings[phase] ?? []
        if (!findings.length) return null
        const hasFailures = findings.some((finding) => finding.passed === false)
        return (
          <details className="checks-panel__phase" key={phase} open={hasFailures || undefined}>
            <summary><span>{label}</span> <span className="checks-panel__count">{findings.length} 项</span></summary>
            <ul className="checks-panel__rules">
              {findings.map((finding, index) => (
                <Rule key={`${finding.rule}:${finding.field}:${index}`} details={{
                  ...finding, name: finding.rule, step: finding.step_key,
                }} />
              ))}
            </ul>
          </details>
        )
      })}
    </article>
  )
}

function requestProblem(error: unknown): Problem {
  const detail = error instanceof ApiError ? error.detail : null
  const code = typeof detail === 'object' && detail !== null && 'code' in detail && typeof detail.code === 'string'
    ? detail.code : 'CHECK_REPORT_LOAD_FAILED'
  return {
    code, phase: 'check', field: 'checks.report', message: '无法读取本机检查报告。',
    observed: error instanceof ApiError ? { http_status: error.status } : undefined,
  }
}

function RunCheckReports({ runId, checkStatus, currentReportId, onRecheck, recheckPending }: ChecksPanelProps) {
  const [reportId, setReportId] = useState('')
  const [result, setResult] = useState<RunChecks | null>(null)
  const [loading, setLoading] = useState(true)
  const [failure, setFailure] = useState<Problem | null>(null)
  const [retry, setRetry] = useState(0)

  useEffect(() => {
    const controller = new AbortController()
    setLoading(true)
    setFailure(null)
    workbenchApi.checks(runId, reportId || undefined, controller.signal)
      .then((snapshot) => {
        if (controller.signal.aborted) return
        if (
          snapshot.run_id !== runId || (snapshot.current_report && snapshot.current_report.run_id !== runId)
          || (reportId && (snapshot.selected_report?.id !== reportId || snapshot.selected_report.run_id !== runId))
        ) throw new Error('Unexpected report reference')
        setResult(snapshot)
      })
      .catch((error: unknown) => {
        if (!controller.signal.aborted) setFailure(requestProblem(error))
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false)
      })
    return () => controller.abort()
  }, [runId, reportId, checkStatus, currentReportId, retry])

  const currentStatus = checkStatus ?? result?.check_status
  const historical = result?.report_ids.filter((id) => id !== result.current_report?.id) ?? []
  const report = reportId
    ? result?.selected_report?.id === reportId ? result.selected_report : null
    : result?.current_report

  return (
    <section className="checks-panel" aria-label="检查报告">
      <header className="checks-panel__header">
        <div role="status" aria-label="当前检查状态">
          <span>当前检查</span>
          <strong className="checks-panel__status" data-status={currentStatus}>
            {currentStatus ? statusLabels[currentStatus] : '待读取'}
          </strong>
        </div>
        {onRecheck && (currentStatus === 'NOT_CHECKED' || recheckPending) && (
          <button type="button" onClick={onRecheck} disabled={recheckPending}>
            {recheckPending ? '正在提交重新检查…' : '重新检查'}
          </button>
        )}
      </header>
      {historical.length > 0 && (
        <label className="checks-panel__history">
          检查报告
          <select value={reportId} onChange={(event) => setReportId(event.currentTarget.value)}>
            <option value="">当前报告</option>
            {historical.map((id) => <option value={id} key={id}>{id}</option>)}
          </select>
        </label>
      )}
      {loading && <p role="status">正在读取检查报告…</p>}
      {failure && (
        <div>
          <ProblemDetails problem={failure} />
          <button type="button" onClick={() => setRetry((value) => value + 1)}>重试读取报告</button>
        </div>
      )}
      {report ? <ReportDetails key={report.id} report={report} historical={Boolean(reportId)} />
        : !loading && !failure && <p>暂无检查报告。</p>}
    </section>
  )
}

export function ChecksPanel(props: ChecksPanelProps) {
  return <RunCheckReports key={props.runId} {...props} />
}
