import { useEffect, useId, useState, type FormEvent } from 'react'
import type { AssetEntry, AssetRef, ContextCriteria, ContextGeometry, Json, MethodEntry, MethodRef, TaskContext, TaskGoal } from '../../api/workbench'
import { artifactTitle } from '../artifacts/ArtifactsPanel'
import { assetKey } from '../assets/AssetPanel'
import { assetMatches, enumLabels, optionFields, optionLabels, optionProblem, parseOptionJson, quantityParts, roleLabels, unitLabels, type OptionField } from './method-form-fields'
import './method-form.css'

export interface MethodFormRequest {
  method: MethodRef
  goal: TaskGoal
  inputs: Record<string, AssetRef>
  critical_options: Record<string, Json>
  criteria?: ContextCriteria
}

export interface MethodFormProps {
  method: MethodEntry
  assets: readonly AssetEntry[]
  goal: string
  inputs: Readonly<Partial<Record<string, AssetRef>>>
  options: Readonly<Partial<Record<string, Json>>>
  context: TaskContext | null
  criteria?: ContextCriteria
  onCriteria?: (value: ContextCriteria) => void
  previewContext?: (request: MethodFormRequest, signal: AbortSignal) => Promise<TaskContext>
  pending?: boolean
  onGoal: (goal: string) => void
  onInput: (role: string, value: AssetRef | null) => void
  onOption: (name: string, value: Json | undefined) => void
  onPrepare: (request: MethodFormRequest) => void
}

const readiness = {
  READY: '可用', CONDITIONAL: '需满足前提', BLOCKED: '不可用', UNKNOWN: '待核验',
} as const

function assetLabel(asset: AssetEntry) {
  const file = asset.source.path.split(/[\\/]/).at(-1) || asset.asset_id
  return [file, asset.source.layer, `v${asset.asset_version}`].filter(Boolean).join(' · ')
}

function exactRef(asset: AssetEntry): AssetRef {
  return { asset_id: asset.asset_id, asset_version: asset.asset_version }
}

const geometryLabels: Record<ContextGeometry, string> = { vector_point: '点', vector_line: '线', vector_polygon: '面' }

function GeometryField({ role, expected, criteria, onCriteria }: {
  role: string; expected: string; criteria: ContextCriteria; onCriteria: (value: ContextCriteria) => void
}) {
  const id = useId()
  const kinds = (Object.keys(geometryLabels) as ContextGeometry[]).filter(kind =>
    expected === 'vector' || expected.slice('vector_'.length).split('_or_').includes(kind.slice('vector_'.length)),
  )
  if (!expected.startsWith('vector') || kinds.length < 2) return null
  return <div className="method-form__field">
    <label htmlFor={id}>对象几何</label>
    <select id={id} name={`criteria.object_types.${role}`} value={criteria.object_types?.[role] ?? ''}
      onChange={event => {
        const next = { ...criteria.object_types }
        const value = event.currentTarget.value as ContextGeometry | ''
        if (value) next[role] = value
        else delete next[role]
        onCriteria({ ...criteria, object_types: next })
      }}>
      <option value="">不限几何</option>
      {kinds.map(kind => <option key={kind} value={kind}>{geometryLabels[kind]}</option>)}
    </select>
  </div>
}

type RoleField = {
  role: string
  expected: string
  candidates: AssetEntry[]
  key: string
  selected: AssetEntry | undefined
  assessment?: TaskContext['roles'][string]
  outsideArtifact?: boolean
}

function AssetField({ field, onInput }: { field: RoleField; onInput: MethodFormProps['onInput'] }) {
  const id = useId()
  const { role, candidates, key, selected, assessment } = field
  const selectedIsCandidate = candidates.some(asset => assetKey(asset) === key)
  const note = selected ? undefined : key ? '所选版本不可用或类型不匹配' : candidates.length ? '待选择' : '暂无匹配资产'
  const showAssessment = assessment && (key || assessment.code !== 'INPUT_REQUIRED') && (selected || assessment.status !== 'READY')
  return (
    <div className="method-form__field">
      <label htmlFor={id}>{roleLabels[role] ?? role}</label>
      <select id={id} name={`inputs.${role}`} required value={key} aria-describedby={note || showAssessment ? `${id}-note` : undefined}
        onChange={event => {
          const asset = candidates.find(candidate => assetKey(candidate) === event.currentTarget.value)
          onInput(role, asset ? exactRef(asset) : null)
        }}
      >
        <option value="">请选择资产</option>
        {key && !selected && <option value={key} disabled>所选版本不可用</option>}
        {selected && !selectedIsCandidate && <option value={key}>{assetLabel(selected)}</option>}
        {candidates.map(asset => <option key={assetKey(asset)} value={assetKey(asset)}>{assetLabel(asset)}</option>)}
      </select>
      {(note || showAssessment) && (
        <small id={`${id}-note`} className="method-form__note">
          {note}
          {showAssessment && (
            <>
              <span data-readiness={assessment.status}>{readiness[assessment.status]}</span>
              {assessment.message && <span>{assessment.message}</span>}
            </>
          )}
        </small>
      )}
      {field.outsideArtifact && <small className="method-form__note">当前成果筛选未用此角色</small>}
    </div>
  )
}

interface OptionEdit { text?: string; committed: Json | undefined; problem?: string }

function OptionControl({ field, id, unit, edit, problem, onChange, onJson, onNumberInput }: {
  field: OptionField; id: string; unit: string | null | undefined; edit?: OptionEdit; problem?: string
  onChange: (value: Json | undefined) => void; onJson: (text: string) => void; onNumberInput: (invalid: boolean) => void
}) {
  const { spec, value, required } = field
  const described = Boolean(unit || problem || (required && value === undefined))
  const attributes = {
    id, name: `critical_options.${field.name}`, required,
    'aria-invalid': problem ? true : undefined, 'aria-describedby': described ? `${id}-note` : undefined,
  }
  if (spec.type === 'enum') {
    return <select {...attributes} value={typeof value === 'string' ? value : ''}
      onChange={event => onChange(event.currentTarget.value || undefined)}>
      <option value="">请选择</option>
      {spec.values?.map(choice => <option key={choice} value={choice}>{enumLabels[choice] ?? choice}</option>)}
    </select>
  }
  if (spec.type === 'boolean') {
    return <select {...attributes} value={typeof value === 'boolean' ? String(value) : ''}
      onChange={event => onChange(event.currentTarget.value === '' ? undefined : event.currentTarget.value === 'true')}>
      <option value="">请选择</option>
      <option value="true">是</option>
      <option value="false">否</option>
    </select>
  }
  if (spec.type === 'number' || spec.type === 'integer') {
    const quantity = spec.unit ? quantityParts(value) : null
    const sameUnit = unit === spec.unit
    return <input {...attributes} type="number" value={quantity?.value ?? (typeof value === 'number' ? value : '')}
      min={sameUnit ? spec.minimum ?? undefined : undefined} max={sameUnit ? spec.maximum ?? undefined : undefined}
      step={spec.type === 'integer' && sameUnit ? 1 : 'any'}
      onInput={event => onNumberInput(event.currentTarget.validity.badInput)}
      onChange={event => {
        const input = event.currentTarget
        if (input.validity.badInput) return
        const number = input.valueAsNumber
        onChange(input.value === '' ? undefined : quantity ? { value: number, unit: quantity.unit } : number)
      }}
    />
  }
  if (spec.type === 'json') {
    const text = edit?.text ?? (value === undefined ? '' : JSON.stringify(value, null, 2))
    const placeholder = field.name === 'threshold'
      ? '{"intervals":[{"class_id":1,"lower":30,"upper":45,"include_lower":true,"include_upper":false}]}'
      : 'JSON 值'
    return <textarea {...attributes} rows={3} value={text} placeholder={placeholder} onChange={event => onJson(event.currentTarget.value)} />
  }
  return <input {...attributes} type="text" value={typeof value === 'string' ? value : ''}
    onChange={event => {
      const text = event.currentTarget.value
      onChange(spec.type === 'reference' && !text.trim() ? undefined : text)
    }}
  />
}

export function OptionInput({ field, problem, edit, onOption, onJson, onNumberInput }: {
  field: OptionField; problem?: string; edit?: OptionEdit; onOption: MethodFormProps['onOption']
  onJson: (text: string) => void; onNumberInput: (invalid: boolean) => void
}) {
  const id = useId()
  const { name, spec, value, required } = field
  const unit = spec.unit ? quantityParts(value)?.unit ?? spec.unit : undefined
  const note = problem ?? (required && value === undefined ? '待填写' : null)
  return (
    <div className="method-form__field">
      <label htmlFor={id}>{optionLabels[name] ?? name}</label>
      <OptionControl
        field={field} id={id} unit={unit} edit={edit} problem={problem} onJson={onJson}
        onChange={value => onOption(name, value)} onNumberInput={onNumberInput}
      />
      {(unit || note) && (
        <small id={`${id}-note`} className="method-form__note">
          {unit && <span>{unitLabels[unit] ?? unit}</span>}
          {note && <span data-invalid={Boolean(problem)}>{note}</span>}
        </small>
      )}
    </div>
  )
}

function MethodFormFields({
  method, assets, goal, inputs, options, context, criteria, onCriteria, previewContext,
  pending, onGoal, onInput, onOption, onPrepare,
}: MethodFormProps) {
  const goalId = useId()
  const scopeId = useId()
  const artifactId = useId()
  const [edits, setEdits] = useState<Record<string, OptionEdit | undefined>>({})
  const [preview, setPreview] = useState<{
    request: string; assets: readonly AssetEntry[]; context?: TaskContext; failed?: boolean
  } | null>(null)
  const { definition } = method
  const matchingContext = context?.method.id === definition.id && context.method.version === definition.version ? context : null
  const fields = optionFields(definition, options)
    .filter(field => !method.derived_options?.includes(field.name))
    .map(field => {
      const stored = edits[field.name]
      const edit = stored && Object.is(stored.committed, options[field.name]) ? stored : undefined
      const problem = edit?.problem ?? optionProblem(field)
      return { field, edit, problem }
    })
  const roleEntries = Object.entries(definition.inputs)
  const declaredInputs = roleEntries.flatMap(([role, expected]): [string, AssetRef][] => {
    const ref = inputs[role]
    if (!ref || (!criteria && !assets.some(asset => assetKey(asset) === assetKey(ref) && assetMatches(asset, expected)))) return []
    return [[role, ref]]
  })
  const declaredOptions = fields.flatMap(({ field: { name, value } }): [string, Json][] => value === undefined ? [] : [[name, value]])
  const request: MethodFormRequest = {
    method: { id: definition.id, version: definition.version }, goal: { summary: goal.trim() },
    inputs: Object.fromEntries(declaredInputs), critical_options: Object.fromEntries(declaredOptions),
    ...(criteria ? { criteria } : {}),
  }
  const requestJson = JSON.stringify(request)
  const hasGoal = Boolean(request.goal.summary)
  const currentPreview = preview?.request === requestJson && preview.assets === assets ? preview : null
  const liveContext = currentPreview?.context ?? (previewContext ? null : matchingContext)
  const shortlist = currentPreview?.context?.candidates ?? preview?.context?.candidates ?? matchingContext?.candidates
  const roles: RoleField[] = roleEntries.map(([role, expected]) => {
    const available = shortlist?.[role]
    const keys = available && new Set(available.flatMap(item => item.asset ? [assetKey(item.asset)] : []))
    const candidates = assets.filter(asset => keys ? keys.has(assetKey(asset)) : assetMatches(asset, expected))
    const ref = inputs[role]
    const key = ref ? assetKey(ref) : ''
    const selected = (criteria ? assets : candidates).find(asset => assetKey(asset) === key)
    const assessment = liveContext?.roles[role]
    const current = assessment && (assessment.asset ? assetKey(assessment.asset) : '') === key
    return { role, expected, candidates, key, selected, assessment: current ? assessment : undefined,
      outsideArtifact: Boolean(shortlist && !(role in shortlist)) }
  })
  const selectableScopes = assets.filter(asset => assetMatches(asset, 'vector_polygon'))
  const scopeKey = criteria?.scope ? assetKey(criteria.scope) : ''
  const expectedArtifacts = definition.expected_artifacts?.map(item => typeof item === 'string' ? item : item.role) ?? []
  const showScope = !('analysis_extent' in definition.inputs) && roleEntries.some(([, expected]) => expected.startsWith('vector') || expected.startsWith('raster'))
  const disabled = Boolean(pending || !method.available || !hasGoal || fields.some(entry => entry.problem))

  useEffect(() => {
    if (!previewContext || !method.available || !hasGoal) return
    const controller = new AbortController()
    const timer = window.setTimeout(() => {
      void previewContext(JSON.parse(requestJson) as MethodFormRequest, controller.signal).then(next => {
        if (!controller.signal.aborted) setPreview({ request: requestJson, assets, context: next })
      }).catch(() => {
        if (!controller.signal.aborted) setPreview({ request: requestJson, assets, failed: true })
      })
    }, 180)
    return () => {
      window.clearTimeout(timer)
      controller.abort()
    }
  }, [assets, hasGoal, method.available, previewContext, requestJson])

  function updateJson(name: string, text: string) {
    if (!text.trim()) {
      setEdits(previous => ({ ...previous, [name]: undefined }))
      onOption(name, undefined)
      return
    }
    let value: Json
    try {
      value = parseOptionJson(text)
    } catch {
      setEdits(previous => ({ ...previous, [name]: { text, committed: options[name], problem: '请输入有效的 JSON 值' } }))
      return
    }
    setEdits(previous => ({ ...previous, [name]: { text, committed: value } }))
    onOption(name, value)
  }

  function checkNumericInput(name: string, invalid: boolean) {
    setEdits(previous => {
      if (!invalid && !previous[name]) return previous
      const edit = invalid ? { committed: options[name], problem: '请输入有限数值' } : undefined
      return { ...previous, [name]: edit }
    })
  }

  function prepare(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    if (disabled) return
    onPrepare(request)
  }

  return (
    <form className="method-form" noValidate onSubmit={prepare} aria-label="方法表单">
      <fieldset disabled={pending || !method.available}>
        <div className="method-form__field">
          <label htmlFor={goalId}>任务目标</label>
          <textarea id={goalId} name="goal.summary" rows={2} required value={goal} onChange={event => onGoal(event.currentTarget.value)} />
        </div>
        {criteria && onCriteria && showScope && <div className="method-form__field">
          <label htmlFor={scopeId}>候选筛选范围</label>
          <select id={scopeId} name="criteria.scope" value={scopeKey} onChange={event => {
            const scope = selectableScopes.find(asset => assetKey(asset) === event.currentTarget.value)
            onCriteria({ ...criteria, scope: scope ? exactRef(scope) : undefined })
          }}>
            <option value="">不限范围</option>
            {scopeKey && !selectableScopes.some(asset => assetKey(asset) === scopeKey) &&
              <option value={scopeKey} disabled>所选范围不可用</option>}
            {selectableScopes.map(asset => <option key={assetKey(asset)} value={assetKey(asset)}>{assetLabel(asset)}</option>)}
          </select>
        </div>}
        {criteria && onCriteria && expectedArtifacts.length > 0 && <div className="method-form__field">
          <label htmlFor={artifactId}>预期成果（候选筛选）</label>
          <select id={artifactId} name="criteria.expected_artifact" value={criteria.expected_artifact ?? ''}
            onChange={event => onCriteria({ ...criteria, expected_artifact: event.currentTarget.value || undefined })}>
            <option value="">全部成果</option>
            {expectedArtifacts.map(role => <option key={role} value={role}>{artifactTitle(role)}</option>)}
          </select>
        </div>}
        {roles.length > 0 && (
          <div className="method-form__group">
            <h4>输入角色</h4>
            {currentPreview?.failed && <p role="status">无法核验本机候选资产。</p>}
            {roles.map(field => <div key={field.role}>
              {criteria && onCriteria && <GeometryField role={field.role} expected={field.expected} criteria={criteria} onCriteria={onCriteria} />}
              <AssetField field={field} onInput={onInput} />
            </div>)}
          </div>
        )}
        {fields.length > 0 && (
          <div className="method-form__group">
            <h4>关键选项</h4>
            {fields.map(({ field, edit, problem }) => (
              <OptionInput
                key={field.name} field={field} edit={edit} problem={problem} onOption={onOption}
                onJson={text => updateJson(field.name, text)} onNumberInput={invalid => checkNumericInput(field.name, invalid)}
              />
            ))}
          </div>
        )}
        <button type="submit" disabled={disabled}>{pending ? '正在生成草稿…' : '生成草稿'}</button>
      </fieldset>
    </form>
  )
}

export function MethodForm(props: MethodFormProps) {
  const { id, version } = props.method.definition
  return <MethodFormFields key={`${id}@${version}`} {...props} />
}
