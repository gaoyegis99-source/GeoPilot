import { useEffect, useId, useState, type FormEvent } from 'react'
import { type AnalysisPlan, type AssetEntry, type ClarificationReport, type Json, type MethodEntry, workbenchApi } from '../../api/workbench'
import { assetKey } from '../assets/AssetPanel'
import { OptionInput } from '../method-form/MethodForm'
import { optionFields, optionLabels, optionProblem, parseOptionJson, roleLabels } from '../method-form/method-form-fields'

type Question = ClarificationReport['items'][number]

interface ClarificationPanelProps {
  plan: AnalysisPlan
  method: MethodEntry
  assets: readonly AssetEntry[]
  disabled: boolean
  onAnswer: (field: string, value: Json) => void
}

function fieldName(question: Question) {
  return question.field.slice(question.field.indexOf('.') + 1)
}

function questionLabel(question: Question) {
  const name = fieldName(question)
  if (question.kind === 'goal') return '任务目标'
  return (question.kind === 'input' ? roleLabels[name] : optionLabels[name]) ?? name
}

function AnswerForm({ question, plan, method, assets, disabled, onAnswer }: ClarificationPanelProps & {
  question: Question
}) {
  const id = useId()
  const name = fieldName(question)
  const [value, setValue] = useState<Json | undefined>(
    question.kind === 'option' ? plan.critical_options[name] : undefined,
  )
  const [asset, setAsset] = useState('')
  const [jsonText, setJsonText] = useState<string>()
  const [inputProblem, setInputProblem] = useState<string>()
  const choices = question.options.filter(choice => typeof choice === 'object')
  const selected = choices.find(choice => assetKey(choice) === asset)
  const option = question.kind === 'option'
    ? optionFields(method.definition, { ...plan.critical_options, [name]: value }).find(field => field.name === name)
    : undefined
  const problem = inputProblem ?? (option && optionProblem(option))
  const answer = question.kind === 'input'
    ? selected && { asset_id: selected.asset_id, asset_version: selected.asset_version }
    : question.kind === 'goal' && typeof value === 'string' ? value.trim() || undefined : option?.value

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    if (!disabled && !problem && answer !== undefined) onAnswer(question.field, answer)
  }

  function changeJson(text: string) {
    setJsonText(text)
    try {
      setValue(text.trim() ? parseOptionJson(text) : undefined)
      setInputProblem(undefined)
    } catch {
      setInputProblem('请输入有效的 JSON 值')
    }
  }

  return <form className="method-form" onSubmit={submit} aria-label="澄清回答">
    <fieldset disabled={disabled}>
      {question.kind === 'goal' && <div className="method-form__field">
        <label htmlFor={id}>补充任务目标</label>
        <textarea id={id} value={typeof value === 'string' ? value : ''} rows={2}
          onChange={event => setValue(event.currentTarget.value)} />
      </div>}
      {question.kind === 'input' && <div className="method-form__field">
        <label htmlFor={id}>{questionLabel(question)}</label>
        <select id={id} value={asset} onChange={event => setAsset(event.currentTarget.value)}>
          <option value="">{choices.length ? '请选择资产版本' : '暂无可选资产版本'}</option>
          {choices.map(choice => {
            const key = assetKey(choice)
            const entry = assets.find(item => assetKey(item) === key)
            const label = entry?.source.path.split(/[\\/]/).at(-1) || choice.asset_id
            return <option key={key} value={key}>
              {[label, entry?.source.layer, `v${choice.asset_version}`].filter(Boolean).join(' · ')}
            </option>
          })}
        </select>
      </div>}
      {option && <OptionInput field={option} problem={problem} edit={{ text: jsonText, committed: value }}
        onOption={(_, next) => setValue(next)} onJson={changeJson}
        onNumberInput={invalid => setInputProblem(invalid ? '请输入有限数值' : undefined)} />}
      <button type="submit" disabled={disabled || Boolean(problem) || answer === undefined}>保存此回答</button>
    </fieldset>
  </form>
}

export function ClarificationPanel(props: ClarificationPanelProps) {
  const { plan, method } = props
  const id = useId()
  const [report, setReport] = useState<ClarificationReport>()
  const [unavailable, setUnavailable] = useState(false)
  const [field, setField] = useState('')
  useEffect(() => {
    const controller = new AbortController()
    workbenchApi.clarification(plan.id, plan.version, controller.signal).then(next => {
      if (!controller.signal.aborted) setReport(next)
    }).catch(() => {
      if (!controller.signal.aborted) setUnavailable(true)
    })
    return () => controller.abort()
  }, [plan.id, plan.version, props.assets])

  if (unavailable) return <p role="status">暂时无法读取澄清问题。可从待补项定位到方法表单。</p>
  if (!report || report.plan_id !== plan.id || report.plan_version !== plan.version || report.task_id !== plan.task_id) return null
  const options = new Set(optionFields(method.definition, plan.critical_options).map(option => option.name))
  const questions = [...new Map(report.items.filter(question =>
    (question.kind === 'goal' && question.field === 'goal.summary')
    || question.kind === 'input'
    || (question.kind === 'option' && options.has(fieldName(question)) && !method.derived_options?.includes(fieldName(question))),
  ).map(question => [question.field, question])).values()]
  const active = questions.find(question => question.field === field) ?? questions[0]
  if (!active) return null

  return <section className="plan-preview" aria-labelledby={id}>
    <h3 id={id}>逐项澄清</h3>
    {questions.length > 1 && <label>待回答字段
      <select value={active.field} onChange={event => setField(event.currentTarget.value)} disabled={props.disabled}>
        {questions.map(question => <option key={question.field} value={question.field}>{questionLabel(question)}</option>)}
      </select>
    </label>}
    <p>{active.reason}</p>
    <AnswerForm key={active.field} {...props} question={active} />
  </section>
}
