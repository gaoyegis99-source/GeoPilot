import { useId } from 'react'
import type { AnalysisPlan, AssetEntry, Json, MethodRef } from '../../../api/workbench'
import { assetKey } from '../../assets/AssetPanel'
import './plan-preview.css'

export interface PlanPreviewProps {
  plan: AnalysisPlan
  assets?: readonly AssetEntry[]
  onIssueSelect?: (field: string) => void
}

function VersionedRef({ id, version }: MethodRef) {
  return <span className="plan-preview__ref"><code>{id}</code> <span>版本 {version}</span></span>
}

function OptionValue({ value }: { value: Json }) {
  if (value === null || typeof value !== 'object') return <code>{JSON.stringify(value)}</code>
  const document = <pre><code>{JSON.stringify(value, null, 2)}</code></pre>
  if (!Array.isArray(value) && typeof value.id === 'string' && typeof value.version === 'number') {
    return <details>
      <summary><VersionedRef id={value.id} version={value.version} /></summary>
      {document}
    </details>
  }
  return document
}

function ValueList({ values, empty }: { values: Record<string, Json>; empty: string }) {
  const entries = Object.entries(values)
  if (entries.length === 0) return <p className="plan-preview__muted">{empty}</p>
  return (
    <dl className="plan-preview__pairs">
      {entries.map(([key, value]) => (
        <div key={key}>
          <dt><code>{key}</code></dt>
          <dd><OptionValue value={value} /></dd>
        </div>
      ))}
    </dl>
  )
}

function StepPreview({ step }: { step: AnalysisPlan['steps'][number] }) {
  const inputs = Object.entries(step.inputs)
  const outputs = Object.entries(step.outputs)
  return (
    <details className="plan-preview__disclosure">
      <summary>
        <code className="plan-preview__step-key">{step.key}</code>
        {step.tool ? <VersionedRef {...step.tool} /> : <span className="plan-preview__muted">待绑定工具</span>}
      </summary>
      <div className="plan-preview__disclosure-body">
        <h5>工具参数</h5>
        <ValueList values={step.parameters} empty="无参数" />
        {inputs.length > 0 && (
          <>
            <h5>步骤输入</h5>
            <dl className="plan-preview__pairs">
              {inputs.map(([port, binding]) => (
                <div key={port}>
                  <dt><code>{port}</code></dt>
                  <dd>{'asset' in binding
                    ? <span>角色 <code>{binding.asset}</code></span>
                    : <span>步骤 <code>{binding.step}.{binding.output}</code></span>}
                  </dd>
                </div>
              ))}
            </dl>
          </>
        )}
        {outputs.length > 0 && (
          <>
            <h5>步骤输出</h5>
            <dl className="plan-preview__pairs">
              {outputs.map(([port, output]) => (
                <div key={port}>
                  <dt><code>{port}</code></dt>
                  <dd className="plan-preview__stack">
                    <code>{output.type}</code>
                    <code>{output.target}</code>
                    {output.layer !== null && <span>图层 <code>{output.layer}</code></span>}
                  </dd>
                </div>
              ))}
            </dl>
          </>
        )}
      </div>
    </details>
  )
}

export function PlanPreview({ plan, assets = [], onIssueSelect }: PlanPreviewProps) {
  const id = useId()
  const exactAssets = new Map(assets.map(asset => [assetKey(asset), asset]))
  const inputs = Object.entries(plan.inputs)

  return (
    <section className="plan-preview" aria-labelledby={`${id}-title`}>
      <header className="plan-preview__header">
        <h3 id={`${id}-title`}>方案预览</h3>
        <span className="plan-preview__status" data-confirmed={plan.status === 'confirmed'}>
          {plan.status === 'confirmed' ? '已确认' : '草稿'}
        </span>
      </header>
      <dl className="plan-preview__pairs">
        <div><dt>方案</dt><dd><VersionedRef id={plan.id} version={plan.version} /></dd></div>
        <div><dt>目标</dt><dd>{plan.goal.summary}</dd></div>
        <div><dt>方法</dt><dd><VersionedRef {...plan.method} /></dd></div>
      </dl>

      {plan.issues.length > 0 && (
        <section aria-labelledby={`${id}-issues`}>
          <h4 id={`${id}-issues`}>待补项</h4>
          <ul className="plan-preview__issues">
            {plan.issues.map((issue, index) => {
              const field = issue.field?.trim() ? issue.field : null
              return (
                <li key={`${issue.code}-${index}`}>
                  <p>{issue.message}</p>
                  <div className="plan-preview__issue-meta">
                    <code>{issue.code}</code>
                    {field && (onIssueSelect
                      ? <button type="button" onClick={() => onIssueSelect(field)}>定位 <code>{field}</code></button>
                      : <code>{field}</code>)}
                  </div>
                </li>
              )
            })}
          </ul>
        </section>
      )}

      <section aria-labelledby={`${id}-inputs`}>
        <h4 id={`${id}-inputs`}>输入角色</h4>
        {inputs.length === 0 ? <p className="plan-preview__muted">暂无已选输入</p> : (
          <dl className="plan-preview__pairs">
            {inputs.map(([role, ref]) => {
              const asset = exactAssets.get(assetKey(ref))
              const file = asset?.source.path.split(/[\\/]/).at(-1)
              return (
                <div key={role}>
                  <dt><code>{role}</code></dt>
                  <dd className="plan-preview__stack">
                    {asset && <span className="plan-preview__asset-name">
                      {file && <span>{file}</span>}
                      {asset.source.layer && <span>{asset.source.layer}</span>}
                    </span>}
                    <VersionedRef id={ref.asset_id} version={ref.asset_version} />
                  </dd>
                </div>
              )
            })}
          </dl>
        )}
      </section>

      <section aria-labelledby={`${id}-options`}>
        <h4 id={`${id}-options`}>关键选项</h4>
        <ValueList values={plan.critical_options} empty="暂无已保存选项" />
      </section>

      <section aria-labelledby={`${id}-steps`}>
        <h4 id={`${id}-steps`}>步骤顺序</h4>
        {plan.steps.length === 0 ? <p className="plan-preview__muted">暂无步骤</p> : (
          <ol className="plan-preview__steps" aria-label="方案步骤">
            {plan.steps.map((step, index) => <li key={`${step.key}-${index}`}><StepPreview step={step} /></li>)}
          </ol>
        )}
      </section>

      {(plan.expected_artifacts.length > 0 || plan.check_rules.length > 0) && (
        <details className="plan-preview__disclosure">
          <summary>成果与检查规则</summary>
          <div className="plan-preview__disclosure-body">
            {plan.expected_artifacts.length > 0 && <>
              <h5>预期成果</h5>
              <dl className="plan-preview__pairs">
                {plan.expected_artifacts.map((artifact, index) => (
                  <div key={`${artifact.role}-${index}`}>
                    <dt><code>{artifact.role}</code></dt>
                    <dd className="plan-preview__stack"><code>{artifact.type}</code><code>{artifact.step}.{artifact.output}</code></dd>
                  </div>
                ))}
              </dl>
            </>}
            {plan.check_rules.length > 0 && <>
              <h5>检查规则</h5>
              <ul className="plan-preview__rules">
                {plan.check_rules.map((rule, index) => <li key={`${rule}-${index}`}><code>{rule}</code></li>)}
              </ul>
            </>}
          </div>
        </details>
      )}
    </section>
  )
}
