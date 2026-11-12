import { useEffect, useRef, useState } from 'react'
import { type ExecutionFeedback, workbenchApi } from '../../api/workbench'

type State =
  | { status: 'idle' }
  | { status: 'loading' }
  | { status: 'error' }
  | { status: 'ready'; report: ExecutionFeedback }

export function ExecutionFeedbackPanel({ runId }: { runId: string }) {
  const [state, setState] = useState<State>({ status: 'idle' })
  const request = useRef<AbortController | null>(null)
  useEffect(() => () => request.current?.abort(), [])

  async function load() {
    request.current?.abort()
    const controller = new AbortController()
    request.current = controller
    setState({ status: 'loading' })
    try {
      const report = await workbenchApi.feedback(runId, controller.signal)
      if (!controller.signal.aborted) setState(report.run_id === runId ? { status: 'ready', report } : { status: 'error' })
    } catch {
      if (!controller.signal.aborted) setState({ status: 'error' })
    }
  }

  if (state.status === 'idle') return <button type="button" onClick={() => { void load() }}>查看执行解释</button>
  if (state.status === 'loading') return <p role="status">正在读取执行解释…</p>
  if (state.status === 'error') return (
    <div>
      <p role="alert">无法读取本机执行解释。</p>
      <button type="button" onClick={() => { void load() }}>重新读取</button>
    </div>
  )
  const { report } = state
  return (
    <section aria-label="执行解释">
      {report.statements.length === 0 && <p>暂无可解释的失败或检查发现。</p>}
      {report.statements.length > 0 && (
        <ul>
          {report.statements.map((statement, index) => {
            const finding = report.findings.find((item) => item.index === statement.check_index)
            const step = report.steps.find((item) => item.step_key === statement.step_key)
            return (
              <li key={index}>
                <strong>{statement.kind === 'fact' ? '记录事实' : '可能原因'}</strong>{' '}
                {statement.step_key && <code>{statement.step_key}</code>}{' '}
                {statement.error_code && <code>{statement.error_code}</code>}{' '}
                {finding && <><code>{finding.rule}</code> · {finding.severity === 'blocking' ? '阻断检查' : '警告检查'}</>}{' '}
                <span>{statement.text}</span>
                {statement.kind === 'fact' && step?.exit_code != null && <> · 退出码 {step.exit_code}</>}
              </li>
            )
          })}
        </ul>
      )}
    </section>
  )
}
