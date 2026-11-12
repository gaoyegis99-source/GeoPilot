import { useId } from 'react'
import type { ModelPreview } from '../../api/workbench'
import { ProblemDetails, type Problem } from '../../components/errors/ProblemDetails'
import './model-assistance.css'

export interface ModelAssistPanelProps {
  available: boolean | null
  pending: boolean
  goal: string
  showGoal: boolean
  suggestion: ModelPreview | null
  problem: Problem | null
  onGoal: (value: string) => void
  onRequest: () => void
  onApply: () => void
  onManual: () => void
}

function SuggestionPreview({ suggestion, goal, pending, onApply }: {
  suggestion: ModelPreview; goal: string; pending: boolean; onApply: () => void
}) {
  const titleId = useId()
  const options = Object.entries(suggestion.options)

  return (
    <section className="model-assist__preview" aria-labelledby={titleId}>
      <h4 id={titleId}>建议预览</h4>
      <div className="model-assist__method">
        <code>{suggestion.method.id}</code><span>版本 {suggestion.method.version}</span>
      </div>
      {suggestion.goal.summary.trim() !== goal.trim() && (
        <div><h5>建议目标</h5><p>{suggestion.goal.summary}</p></div>
      )}
      {options.length > 0 && <dl className="model-assist__options">
        {options.map(([name, value]) => (
          <div key={name}>
            <dt><code>{name}</code></dt>
            <dd>{value !== null && typeof value === 'object'
              ? <pre><code>{JSON.stringify(value, null, 2)}</code></pre>
              : <code>{JSON.stringify(value)}</code>}
            </dd>
          </div>
        ))}
      </dl>}
      {suggestion.missing.length > 0 && (
        <div>
          <h5>待补选项</h5>
          <ul>{suggestion.missing.map((name, index) => <li key={`${name}-${index}`}><code>{name}</code></li>)}</ul>
        </div>
      )}
      {suggestion.issues.length > 0 && (
        <ul aria-label="方案问题" className="model-assist__issues">
          {suggestion.issues.map((issue, index) => (
            <li key={`${issue.code}-${index}`}>
              <p>{issue.message}</p>
              {issue.field && <code>{issue.field}</code>}
            </li>
          ))}
        </ul>
      )}
      {suggestion.explanation && <p className="model-assist__explanation">{suggestion.explanation}</p>}
      {suggestion.explanation_issue && <p className="model-assist__muted">{suggestion.explanation_issue.message}</p>}
      <button type="button" className="model-assist__primary" disabled={pending} onClick={onApply}>应用建议</button>
    </section>
  )
}

export function ModelAssistPanel({
  available, pending, goal, showGoal, suggestion, problem, onGoal, onRequest, onApply, onManual,
}: ModelAssistPanelProps) {
  const id = useId()

  return (
    <section className="model-assist" aria-labelledby={`${id}-title`}>
      <h3 id={`${id}-title`}>模型辅助</h3>
      {showGoal && (
        <div className="model-assist__goal">
          <label htmlFor={`${id}-goal`}>任务目标</label>
          <textarea id={`${id}-goal`} name="goal.summary" rows={2} value={goal} onChange={event => onGoal(event.currentTarget.value)} />
        </div>
      )}
      {available !== true && (
        <p role="status" className="model-assist__muted">
          {available === null ? '正在检查模型辅助…' : '模型辅助未配置'}
        </p>
      )}
      <div className="model-assist__actions">
        <button type="button" className="model-assist__primary" onClick={onRequest} disabled={!available || pending || !goal.trim()}>
          {pending ? '正在获取建议…' : '获取建议'}
        </button>
        <button type="button" onClick={onManual}>手工填写</button>
      </div>
      {problem && <ProblemDetails problem={problem} />}
      {suggestion && <SuggestionPreview suggestion={suggestion} goal={goal} pending={pending} onApply={onApply} />}
    </section>
  )
}
