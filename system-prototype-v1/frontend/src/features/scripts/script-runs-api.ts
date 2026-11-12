import { ApiError, type RunError } from '../../api/workbench'

export type ScriptRunStatus = 'NOT_STARTED' | 'RESULT_PENDING' | 'SUCCEEDED' | 'FAILED'

export interface ScriptRunSummary {
  script_run_id: string
  script_id: string
  version: number
  created_at: string
  status: ScriptRunStatus
  log_available: boolean
}

export interface ScriptOutput {
  index: number
  path: string
  size_bytes: number
  readable: boolean
  preview_available: boolean
}

export interface ScriptRunView extends ScriptRunSummary {
  exit_code: number | null
  timed_out: boolean
  started_at: string | null
  finished_at: string | null
  error: RunError | null
  outputs: ScriptOutput[]
}

const collection = '/api/script-runs'
const runUrl = (id: string) => `${collection}/${encodeURIComponent(id)}`
const logUrl = (id: string) => `${runUrl(id)}/log`
const outputUrl = (id: string, index: number) => `${runUrl(id)}/outputs/${index}`

async function read(url: string, signal: AbortSignal) {
  const response = await fetch(url, { signal, redirect: 'error' })
  if (response.ok) return response
  const payload: { detail?: unknown } | null = await response.json().catch(() => null)
  throw new ApiError(response.status, payload?.detail)
}

export const scriptRunsApi = {
  async list(signal: AbortSignal): Promise<{ runs: ScriptRunSummary[] }> {
    return (await read(collection, signal)).json()
  },
  async run(id: string, signal: AbortSignal): Promise<ScriptRunView> {
    return (await read(runUrl(id), signal)).json()
  },
  async log(id: string, signal: AbortSignal): Promise<string> {
    return (await read(`${logUrl(id)}?tail=200`, signal)).text()
  },
  logUrl,
  fileUrl: (id: string, index: number) => `${outputUrl(id, index)}/file`,
  previewUrl: (id: string, index: number) => `${outputUrl(id, index)}/preview`,
}
