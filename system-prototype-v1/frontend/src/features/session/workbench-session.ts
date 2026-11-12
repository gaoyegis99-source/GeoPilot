import { type AnalysisPlan, type AssetRef, type MethodRef, type TaskRecord, workbenchApi } from '../../api/workbench'

export class SessionLinkError extends Error {}

interface SessionSelection {
  taskId?: string | null
  plan?: MethodRef | null
  runId?: string | null
}

interface WorkbenchSession {
  task: TaskRecord
  plan: AnalysisPlan | null
  run: TaskRecord['runs'][number] | null
}

export function hasWorkbenchLocation(search: string) {
  const params = new URLSearchParams(search)
  return ['task', 'plan', 'version', 'run'].some((key) => params.has(key))
}

export function workbenchLocation(base: URL, { taskId, plan, runId }: SessionSelection) {
  const next = new URL(base)
  const values = { task: taskId, plan: plan?.id, version: plan && String(plan.version), run: runId }
  for (const [key, value] of Object.entries(values)) {
    if (value) next.searchParams.set(key, value)
    else next.searchParams.delete(key)
  }
  return next
}

export function readMapLayers(search: string): AssetRef[] {
  const values = new Set(new URLSearchParams(search).getAll('layer'))
  return [...values].flatMap((value) => {
    const match = /^(.*)@([1-9]\d*)$/.exec(value)
    const version = Number(match?.[2])
    return match?.[1] && Number.isSafeInteger(version) ? [{ asset_id: match[1], asset_version: version }] : []
  })
}

export function mapLayersLocation(base: URL, layers: readonly AssetRef[]) {
  const next = new URL(base)
  next.searchParams.delete('layer')
  for (const asset of layers) next.searchParams.append('layer', `${asset.asset_id}@${asset.asset_version}`)
  return next
}

export async function readWorkbenchSession(search: string, signal?: AbortSignal): Promise<WorkbenchSession | null> {
  if (!hasWorkbenchLocation(search)) return null
  const params = new URLSearchParams(search)
  const runId = params.get('run')
  const planId = params.get('plan')
  const versionText = params.get('version') ?? ''
  const version = Number(versionText)
  if ((params.has('plan') || params.has('version'))
      && (!planId || !/^[1-9]\d*$/.test(versionText) || !Number.isSafeInteger(version))) {
    throw new SessionLinkError('方案链接缺少有效版本。')
  }

  const origin = runId ? await workbenchApi.runOrigin(runId, signal) : null
  const taskId = params.get('task') ?? origin?.task_id
  if (!taskId) throw new SessionLinkError('链接缺少有效任务编号。')
  if (origin && (origin.run_id !== runId || origin.task_id !== taskId)) {
    throw new SessionLinkError('运行不属于此任务。')
  }

  const task = await workbenchApi.task(taskId, signal)
  if (task.task_id !== taskId) throw new SessionLinkError('服务返回了其他任务记录。')
  const planLink = planId ? task.plans.find((entry) => entry.id === planId && entry.version === version)
    : origin ? task.plans.find((entry) => entry.id === origin.plan_id && entry.version === origin.plan_version)
      : task.plans[0]
  if ((planId || origin) && !planLink) throw new SessionLinkError('方案版本不属于此任务。')

  const run = origin
    ? task.runs.find((entry) => entry.run_id === origin.run_id
        && entry.plan_id === origin.plan_id && entry.plan_version === origin.plan_version)
    : planLink && task.runs.find((entry) => entry.plan_id === planLink.id && entry.plan_version === planLink.version)
  if (origin && !run) throw new SessionLinkError('运行与任务的方案版本不一致。')

  const plan = planLink ? await workbenchApi.plan(planLink.id, planLink.version, signal) : null
  if (plan && (plan.task_id !== taskId || plan.id !== planLink?.id || plan.version !== planLink.version)) {
    throw new SessionLinkError('服务返回了其他方案版本。')
  }
  return { task, plan, run: run ?? null }
}
