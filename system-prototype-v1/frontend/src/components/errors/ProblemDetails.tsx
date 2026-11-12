import { ApiError, type Json } from '../../api/workbench'
import './problem.css'

export interface Problem {
  code: string
  message: string
  phase?: string
  step_key?: string
  field?: string | null
  suggestion?: string
  observed?: Json
}

export function requestProblem(cause: unknown, phase: string, fallback: string): Problem {
  const detail = cause instanceof ApiError ? cause.detail : null
  const fields = detail && typeof detail === 'object' ? detail as Record<string, unknown> : {}
  return {
    phase,
    code: typeof fields.code === 'string' ? fields.code : 'REQUEST_FAILED',
    field: typeof fields.field === 'string' ? fields.field : undefined,
    message: cause instanceof ApiError ? cause.message : fallback,
    suggestion: typeof fields.suggestion === 'string' ? fields.suggestion : undefined,
  }
}

type Action = 'edit' | 'retry' | 'recheck' | 'preview'
export type ProblemActions = Partial<Record<Action, () => void>>

const actionLabels: Record<Action, string> = {
  edit: '返回草稿', retry: '重新运行', recheck: '重新检查', preview: '重新加载预览',
}
const phaseLabels: Record<string, string> = {
  data: '输入数据', preflight: '执行前检查', execute: '计算执行', check: '规则检查',
  preview: '地图预览', plan: '方案', output: '成果验证', model: '模型建议',
}

function relevantAction(problem: Problem): Action {
  if (problem.phase === 'preview') return 'preview'
  if (problem.phase === 'check') return problem.code === 'CHECK_BLOCKED' ? 'edit' : 'recheck'
  if (problem.phase === 'plan' || problem.phase === 'preflight' || problem.phase === 'data') return 'edit'
  return 'retry'
}

export function ProblemDetails({ problem, actions = {}, logHref }: {
  problem: Problem
  actions?: ProblemActions
  logHref?: string
}) {
  const action = relevantAction(problem)
  return (
    <div className="workbench-problem">
      <div role="alert">
        {problem.phase && <span className="workbench-problem__phase">{phaseLabels[problem.phase] ?? problem.phase}</span>}
        <p>{problem.message}</p>
        {problem.suggestion && <p className="workbench-problem__suggestion">{problem.suggestion}</p>}
      </div>
      <div className="workbench-problem__actions">
        {actions[action] && <button type="button" onClick={() => actions[action]?.()}>{actionLabels[action]}</button>}
        {logHref && <a href={logHref} target="_blank" rel="noreferrer">查看完整日志</a>}
      </div>
      <details>
        <summary>技术详情</summary>
        <dl>
          <dt>错误代码</dt><dd><code>{problem.code}</code></dd>
          {problem.step_key && <><dt>步骤</dt><dd><code>{problem.step_key}</code></dd></>}
          {problem.field && <><dt>字段</dt><dd><code>{problem.field}</code></dd></>}
        </dl>
        {problem.observed != null && <pre>{JSON.stringify(problem.observed, null, 2)}</pre>}
      </details>
    </div>
  )
}
