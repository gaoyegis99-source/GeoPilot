import { useEffect, useId, useState, type ReactNode } from 'react'
import type { PlanAlternatives, PlanComparison } from '../../api/plan-alternatives'
import type { AnalysisPlan, Json } from '../../api/workbench'
import { enumLabels, optionLabels, roleLabels } from '../method-form/method-form-fields'
import './preview/plan-preview.css'

interface AlternativePlansPanelProps {
  plan: AnalysisPlan
  disabled: boolean
  load: (id: string, version: number, signal: AbortSignal) => Promise<PlanAlternatives>
  onSelect: (choice: PlanComparison) => void
}

function keys<T, U>(left: Record<string, T>, right: Record<string, U>) {
  return [...new Set([...Object.keys(left), ...Object.keys(right)])]
}

function DisplayValue({ value, missing = '待填写' }: { value: Json | undefined; missing?: string }) {
  if (value === undefined) return <span>{missing}</span>
  return <code>{typeof value === 'string' ? enumLabels[value] ?? value : JSON.stringify(value)}</code>
}

function Input({ comparison, role }: { comparison: PlanComparison; role: string }) {
  const kind = comparison.input_types[role]
  if (!kind) return <span>无此角色</span>
  const ref = comparison.plan.inputs[role]
  return <><code>{kind}</code>{ref
    ? <> · <code>{ref.asset_id}</code> · v{ref.asset_version}</>
    : <> · 待选择</>}</>
}

function Option({ comparison, name, other }: { comparison: PlanComparison; name: string; other: PlanComparison }) {
  const spec = comparison.option_specs[name]
  if (spec === undefined) return <span>无此参数</span>
  return <>
    <DisplayValue value={comparison.plan.critical_options[name]} />
    {JSON.stringify(spec) !== JSON.stringify(other.option_specs[name]) && (
      <details><summary>参数约束</summary><DisplayValue value={spec} /></details>
    )}
  </>
}

function Table({ title, rows }: { title: string; rows: Array<[string, string, ReactNode, ReactNode]> }) {
  if (!rows.length) return null
  return <table className="plan-preview__comparison">
    <caption>{title}</caption>
    <thead><tr><th scope="col">字段</th><th scope="col">当前方案</th><th scope="col">替代方案</th></tr></thead>
    <tbody>{rows.map(([key, label, current, alternative]) => <tr key={key}>
      <th scope="row">{label}</th><td>{current}</td><td>{alternative}</td>
    </tr>)}</tbody>
  </table>
}

function Constraints({ comparison }: { comparison: PlanComparison }) {
  return <ul>
    {comparison.preconditions.map(rule => <li key={`precondition-${rule}`}><code>{rule}</code></li>)}
    {comparison.plan.check_rules.map(rule => <li key={`check-${rule}`}><code>{rule}</code></li>)}
    {Object.entries(comparison.measurement).map(([step, rule]) => <li key={`measurement-${step}`}>
      <code>{step}</code> <DisplayValue value={rule} />
    </li>)}
  </ul>
}

function AlternativeComparison({ baseline, choice }: { baseline: PlanComparison; choice: PlanComparison }) {
  const tools = (plan: AnalysisPlan) => Object.fromEntries(plan.steps.map(({ key, tool }) => [
    key, tool && `${tool.id} v${tool.version}`,
  ]))
  const before = tools(baseline.plan)
  const after = tools(choice.plan)
  const artifacts = (plan: AnalysisPlan) => Object.fromEntries(plan.expected_artifacts.map(item => [item.role, item.type]))
  const beforeArtifacts = artifacts(baseline.plan)
  const afterArtifacts = artifacts(choice.plan)
  return <>
    <Table title="输入角色和资产版本" rows={keys(baseline.input_types, choice.input_types).map(role => [
      role, roleLabels[role] ?? role, <Input comparison={baseline} role={role} />, <Input comparison={choice} role={role} />,
    ])} />
    <Table title="关键参数" rows={keys(baseline.option_specs, choice.option_specs).map(name => [
      name, optionLabels[name] ?? name,
      <Option comparison={baseline} other={choice} name={name} />,
      <Option comparison={choice} other={baseline} name={name} />,
    ])} />
    <Table title="工具变更" rows={keys(before, after).filter(step => before[step] !== after[step]).map(step => [
      step, step, <DisplayValue value={before[step]} missing="无此步骤" />, <DisplayValue value={after[step]} missing="无此步骤" />,
    ])} />
    <Table title="成果变更" rows={keys(beforeArtifacts, afterArtifacts)
      .filter(role => beforeArtifacts[role] !== afterArtifacts[role]).map(role => [
        role, role, <DisplayValue value={beforeArtifacts[role]} missing="无此成果" />,
        <DisplayValue value={afterArtifacts[role]} missing="无此成果" />,
      ])} />
    <details>
      <summary>前置条件、测量与检查规则</summary>
      <Table title="方案约束" rows={[
        ['rules', '执行约束', <Constraints comparison={baseline} />, <Constraints comparison={choice} />],
      ]} />
    </details>
    {choice.plan.issues.length > 0 && <ul aria-label="替代方案待补项">
      {choice.plan.issues.map((issue, index) => <li key={`${issue.code}-${index}`}>{issue.message}</li>)}
    </ul>}
  </>
}

function choiceLabel(choice: PlanComparison, baseline: PlanComparison) {
  const { method, critical_options: options } = choice.plan
  const changedMode = Object.entries(options).find(([name, value]) => {
    const spec = choice.option_specs[name]
    return value !== baseline.plan.critical_options[name] && typeof value === 'string'
      && spec && typeof spec === 'object' && !Array.isArray(spec) && spec.type === 'enum'
  })?.[1]
  return `${method.id} · v${method.version}${typeof changedMode === 'string' ? ` · ${enumLabels[changedMode] ?? changedMode}` : ''}`
}

export function AlternativePlansPanel({ plan, disabled, load, onSelect }: AlternativePlansPanelProps) {
  const id = useId()
  const [open, setOpen] = useState(false)
  const [report, setReport] = useState<PlanAlternatives>()
  const [failed, setFailed] = useState(false)
  const [selection, setSelection] = useState(0)
  useEffect(() => {
    if (!open) return
    const abort = new AbortController()
    setFailed(false)
    load(plan.id, plan.version, abort.signal).then(next => {
      if (!abort.signal.aborted) setReport(next)
    }).catch(() => {
      if (!abort.signal.aborted) setFailed(true)
    })
    return () => abort.abort()
  }, [load, open, plan.id, plan.version])
  const choice = report?.alternatives[selection] ?? report?.alternatives[0]
  const valid = report?.baseline.plan.id === plan.id && report.baseline.plan.version === plan.version
  return <details className="plan-preview plan-preview__alternatives" onToggle={event => setOpen(event.currentTarget.open)}>
    <summary>比较替代方案</summary>
    {open && failed && <p role="status">暂时无法读取替代方案。</p>}
    {open && !failed && !valid && <p role="status">正在检查已登记的方法…</p>}
    {open && valid && report && !failed && (choice ? <>
      <label htmlFor={id}>替代方案</label>
      <select id={id} value={selection} onChange={event => setSelection(Number(event.currentTarget.value))}>
        {report.alternatives.map((item, index) => <option key={index} value={index}>{choiceLabel(item, report.baseline)}</option>)}
      </select>
      <AlternativeComparison baseline={report.baseline} choice={choice} />
      <button type="button" disabled={disabled} onClick={() => onSelect(choice)}>采用并生成草稿</button>
    </> : <p>当前目录没有符合该任务的替代方法。</p>)}
  </details>
}
