import type { FailureReplanning, PlanAlternatives } from './plan-alternatives'

export type Json = null | boolean | number | string | Json[] | { [key: string]: Json }
export type ExecutionStatus = 'NOT_STARTED' | 'RUNNING' | 'SUCCEEDED' | 'FAILED' | 'CANCELLED'
export type CheckStatus = 'NOT_CHECKED' | 'CHECKING' | 'PASSED' | 'WARNING' | 'FAILED'

export interface Issue {
  code: string
  field: string | null
  message: string
}

export interface RunError extends Issue {
  phase: string
  step_key: string
  suggestion: string
  log_path: string | null
  observed?: Json
}

export interface AssetRef {
  asset_id: string
  asset_version: number
}

export interface AssetEntry extends AssetRef {
  source: { path: string; layer: string | null }
  source_type: string
  availability: 'AVAILABLE' | 'UNAVAILABLE'
  created_at: string
  metadata: {
    kind: 'vector' | 'raster' | 'table'
    format: string
    crs?: string | null
    bounds?: [number, number, number, number] | null
    geometry_type?: string
    feature_count?: number
    row_count?: number
    width?: number
    height?: number
    issues?: string[]
  }
}

export interface LayerEntry {
  name: string
  geometry_type: string | null
}

export interface MethodRef {
  id: string
  version: number
}

export interface OptionSpec {
  type: 'enum' | 'number' | 'integer' | 'boolean' | 'string' | 'json' | 'reference'
  values?: string[] | null
  required?: boolean
  minimum?: number | null
  maximum?: number | null
  minimum_exclusive?: number | null
  maximum_exclusive?: number | null
  unit?: string | null
  default?: Json
  required_for?: string | null
}

type MethodParameterBindings = Record<string, { option: string } | { literal: Json }>

export interface MethodEntry {
  title: string
  description: string
  available: boolean
  issues: Issue[]
  derived_options?: string[]
  definition: MethodRef & {
    locked_order: boolean
    inputs: Record<string, string>
    critical_options: Record<string, OptionSpec | 'user_defined' | 'user_selected'>
    steps: Array<{
      key: string
      tool: MethodRef | null
      parameters?: MethodParameterBindings
    }>
    step_variants?: {
      option: string
      choices: Record<string, Record<string, { tool: MethodRef; parameters?: MethodParameterBindings }>>
    } | null
    expected_artifacts?: Array<string | { role: string; step: string; output: string; type: string }>
    check_rules: string[]
  }
}

export interface TaskGoal { summary: string }

export interface ModelPreview {
  method: MethodRef
  goal: TaskGoal
  options: Record<string, Json>
  missing: string[]
  explanation: string | null
  explanation_issue: Issue | null
  issues: Issue[]
}

export type ContextGeometry = 'vector_point' | 'vector_line' | 'vector_polygon'

export interface ContextCriteria {
  scope?: AssetRef | null
  object_types?: Record<string, ContextGeometry>
  expected_artifact?: string | null
}

export interface InputAssessment {
  asset: AssetRef | null
  status: 'READY' | 'CONDITIONAL' | 'BLOCKED' | 'UNKNOWN'
  code: string | null
  message: string | null
}

export interface TaskContext {
  task_id: string
  goal: TaskGoal
  method: MethodRef
  roles: Record<string, InputAssessment>
  criteria?: ContextCriteria | null
  candidates?: Record<string, InputAssessment[]> | null
}

export interface ContextRequest {
  method: MethodRef
  inputs: Record<string, AssetRef>
  goal?: TaskGoal
  critical_options?: Record<string, Json>
  criteria?: ContextCriteria
}

export interface PlanLink extends MethodRef { status: 'draft' | 'confirmed' }

export interface AnalysisPlan extends PlanLink {
  task_id: string
  goal: TaskGoal
  method: MethodRef
  inputs: Record<string, AssetRef>
  critical_options: Record<string, Json>
  steps: Array<{
    key: string
    tool: MethodRef | null
    inputs: Record<string, { asset: string } | { step: string; output: string }>
    parameters: Record<string, Json>
    outputs: Record<string, { type: string; target: string; layer: string | null }>
  }>
  expected_artifacts: Array<{ role: string; step: string; output: string; type: string }>
  check_rules: string[]
  issues: Issue[]
}

export interface ClarificationReport {
  schema_version: 1
  task_id: string
  plan_id: string
  plan_version: number
  method: MethodRef
  status: 'blocked' | 'ready_for_confirmation' | 'confirmed'
  items: Array<{
    kind: 'goal' | 'input' | 'option' | 'validation' | 'confirmation'
    field: string
    code: string
    reason: string
    options: Array<AssetRef | string | boolean>
  }>
}

export interface TaskRecord {
  task_id: string
  goal: TaskGoal
  context: TaskContext | null
  plans: PlanLink[]
  runs: Array<{
    run_id: string
    plan_id: string
    plan_version: number
    execution_status: ExecutionStatus
    check_status: CheckStatus
  }>
}

export interface RunOrigin {
  run_id: string
  task_id: string
  plan_id: string
  plan_version: number
}

export interface RunStatus {
  run_id: string
  execution_status: ExecutionStatus
  check_status: CheckStatus
  current_step: string | null
  cancel_requested: boolean
  current_report_id: string | null
  error: RunError | null
}

export interface RunStep {
  step_key: string
  position: number
  status: ExecutionStatus
  log_available: boolean
  started_at: string | null
  finished_at: string | null
  error: RunError | null
}

export interface ExecutionFeedback {
  run_id: string
  steps: Array<{ step_key: string; exit_code: number | null }>
  findings: Array<{ index: number; rule: string; severity: 'blocking' | 'warning' }>
  statements: Array<{
    kind: 'fact' | 'inference'
    text: string
    step_key: string | null
    error_code: string | null
    check_index: number | null
  }>
}

export interface ArtifactEntry {
  id: string
  run_id: string
  role: string
  uri: string
  layer: string | null
  type: string
  kind: 'vector' | 'raster' | 'table' | 'json'
  format: string
  fingerprint: string
  metadata: Record<string, Json>
  source: { plan_id: string; plan_version: number; step_key: string; output_name: string; tool: MethodRef; inputs: Record<string, AssetRef> }
  created_at: string
  readable: boolean
}

export interface DiagnosticFile {
  step_key: string
  role: string
  type: string
  path: string
  layer: string | null
  readable: boolean
}

export interface RunArtifacts {
  run_id: string
  check_status: CheckStatus
  artifacts: ArtifactEntry[]
  diagnostics: DiagnosticFile[]
}

export interface ArtifactTable {
  columns: string[]
  rows: string[][]
  truncated: boolean
}

export interface RasterValueQuery {
  longitude: number
  latitude: number
  band: number
  source_fingerprint: string
}

export type RasterPixelValue = RasterValueQuery & {
  artifact_id: string
  source_crs: string
  source_x: number
  source_y: number
  dtype: string
  unit: string | null
  scale: number
  offset: number
} & (
  | { status: 'value'; row: number; column: number; nodata: false; value: number | string }
  | { status: 'nodata'; row: number; column: number; nodata: true; value: null }
  | { status: 'outside'; row: null; column: null; nodata: false; value: null }
)

export interface CheckFinding {
  rule: string
  phase: 'input' | 'process' | 'artifact'
  step_key?: string | null
  field: string
  severity: 'blocking' | 'warning'
  passed: boolean | null
  observed: Json
  expected: Json
  message: string
}

export interface CheckReport {
  id: string
  run_id: string
  status: CheckStatus
  rule_versions: Record<string, number>
  created_at: string
  report: {
    completed_rules: string[]
    pending_rules: string[]
    interruption: Issue | null
    phase_findings: Partial<Record<CheckFinding['phase'], CheckFinding[]>>
    rules: Record<string, { passed: boolean | null; severity: string; message: string; observed: Json; expected?: Json }>
  }
}

export interface RunChecks {
  run_id: string
  check_status: CheckStatus
  current_report: CheckReport | null
  report_ids: string[]
  selected_report?: CheckReport
}

export class ApiError extends Error {
  constructor(readonly status: number, readonly detail: unknown) {
    super(
      typeof detail === 'object' && detail !== null && 'message' in detail && typeof detail.message === 'string'
        ? detail.message
        : typeof detail === 'string' ? detail : `请求失败（HTTP ${status}）`,
    )
    this.name = 'ApiError'
  }
}

const segment = encodeURIComponent
const taskUrl = (id: string) => `/api/tasks/${segment(id)}`
const planUrl = (id: string) => `/api/plans/${segment(id)}`
const runUrl = (id: string) => `/api/runs/${segment(id)}`
const artifactUrl = (id: string) => `/api/artifacts/${segment(id)}`

async function fetchResponse(url: string, init?: RequestInit) {
  const response = await fetch(url, init)
  if (response.ok) return response
  const payload: { detail?: unknown } | null = await response.json().catch(() => null)
  throw new ApiError(response.status, payload?.detail)
}

async function readJson<T>(url: string, signal?: AbortSignal): Promise<T> {
  return (await fetchResponse(url, { signal })).json()
}

async function sendJson<T>(url: string, body?: unknown, signal?: AbortSignal): Promise<T> {
  const init: RequestInit = { method: 'POST', signal }
  if (body !== undefined) {
    init.headers = { 'Content-Type': 'application/json' }
    init.body = JSON.stringify(body)
  }
  return (await fetchResponse(url, init)).json()
}

export const workbenchApi = {
  assets: (signal?: AbortSignal) => readJson<{ assets: AssetEntry[] }>('/api/assets', signal),
  layers: (path: string, signal?: AbortSignal) =>
    readJson<{ layers: LayerEntry[] }>(`/api/assets/layers?path=${segment(path)}`, signal),
  register: (path: string, layer?: string) => sendJson<AssetEntry>('/api/assets', { path, layer }),
  assetPreviewUrl: (asset: AssetRef) => `/api/assets/${segment(asset.asset_id)}/preview?version=${asset.asset_version}`,
  methods: (signal?: AbortSignal) => readJson<{ methods: MethodEntry[] }>('/api/methods', signal),
  modelAvailability: (signal?: AbortSignal) => readJson<{ available: boolean }>('/api/model-assistance', signal),
  modelSuggestion: (request: {
    goal: TaskGoal; method?: MethodRef; inputs: Record<string, AssetRef>; protected_literals?: string[]
  }, signal?: AbortSignal) => sendJson<ModelPreview>('/api/model-assistance', request, signal),
  createTask: (goal: TaskGoal) => sendJson<{ task_id: string }>('/api/tasks', { goal }),
  task: (id: string, signal?: AbortSignal) => readJson<TaskRecord>(taskUrl(id), signal),
  previewContext: (request: ContextRequest & { goal: TaskGoal }, signal?: AbortSignal) =>
    sendJson<TaskContext>('/api/tasks/context-preview', request, signal),
  context: (id: string, request: ContextRequest) => sendJson<TaskContext>(`${taskUrl(id)}/context`, request),
  draft: (id: string, request: ContextRequest & {
    critical_options: Record<string, Json>; base_plan_id?: string; base_plan_version?: number
  }) => sendJson<AnalysisPlan>(`${taskUrl(id)}/plans`, request),
  plan: (id: string, version: number, signal?: AbortSignal) =>
    readJson<AnalysisPlan>(`${planUrl(id)}?version=${version}`, signal),
  alternatives: (id: string, version: number, signal?: AbortSignal) =>
    readJson<PlanAlternatives>(`${planUrl(id)}/alternatives?version=${version}`, signal),
  clarification: (id: string, version: number, signal?: AbortSignal) =>
    readJson<ClarificationReport>(`${planUrl(id)}/clarification?version=${version}`, signal),
  answerClarification: (id: string, version: number, field: string, value: Json) =>
    sendJson<AnalysisPlan>(`${planUrl(id)}/clarification`, { plan_version: version, field, value }),
  confirm: (id: string, version: number) => sendJson<AnalysisPlan>(`${planUrl(id)}/confirm`, { plan_version: version }),
  start: (id: string, version: number) => sendJson<{ run_id: string }>(`${planUrl(id)}/runs`, { plan_version: version }),
  run: (id: string, signal?: AbortSignal) => readJson<RunStatus>(runUrl(id), signal),
  runOrigin: (id: string, signal?: AbortSignal) => readJson<RunOrigin>(`${runUrl(id)}/origin`, signal),
  feedback: (id: string, signal?: AbortSignal) => readJson<ExecutionFeedback>(`${runUrl(id)}/feedback`, signal),
  replanning: (id: string, signal?: AbortSignal) => readJson<FailureReplanning>(`${runUrl(id)}/replanning`, signal),
  applyReplan: (id: string, proposalId: string) =>
    sendJson<AnalysisPlan>(`${runUrl(id)}/replanning`, { proposal_id: proposalId }),
  cancel: (id: string) => sendJson<RunStatus>(`${runUrl(id)}/cancel`),
  steps: (id: string, signal?: AbortSignal) => readJson<{ run_id: string; steps: RunStep[] }>(`${runUrl(id)}/steps`, signal),
  logUrl: (id: string, step: string) => `${runUrl(id)}/steps/${segment(step)}/log`,
  async log(id: string, step: string, tail: number, signal?: AbortSignal) {
    return (await fetchResponse(`${workbenchApi.logUrl(id, step)}?tail=${tail}`, { signal })).text()
  },
  artifacts: (id: string, signal?: AbortSignal) => readJson<RunArtifacts>(`${runUrl(id)}/artifacts`, signal),
  artifactPreviewUrl: (id: string) => `${artifactUrl(id)}/preview`,
  artifactRasterPreviewUrl: (id: string) => `${artifactUrl(id)}/raster-preview`,
  async artifactValue(id: string, query: RasterValueQuery, signal?: AbortSignal): Promise<RasterPixelValue> {
    const parameters = new URLSearchParams({
      longitude: String(query.longitude), latitude: String(query.latitude), band: String(query.band),
      source_fingerprint: query.source_fingerprint,
    })
    const response = await fetchResponse(`${artifactUrl(id)}/value?${parameters}`, { signal, redirect: 'error', cache: 'no-store' })
    return response.json()
  },
  artifactFileUrl: (id: string) => `${artifactUrl(id)}/file`,
  artifactTable: (id: string) => readJson<ArtifactTable>(`${artifactUrl(id)}/table`),
  artifactDocument: (id: string) => readJson<Json>(`${artifactUrl(id)}/file`),
  checks: (id: string, reportId?: string, signal?: AbortSignal) =>
    readJson<RunChecks>(`${runUrl(id)}/checks${reportId ? `?report_id=${segment(reportId)}` : ''}`, signal),
  recheck: (id: string) => sendJson<{ report_id: string }>(`${runUrl(id)}/checks`),
}
