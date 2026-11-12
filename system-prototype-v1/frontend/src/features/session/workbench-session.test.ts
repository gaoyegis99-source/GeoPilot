import { afterEach, expect, it, vi } from 'vitest'
import { type AnalysisPlan, type TaskRecord, workbenchApi } from '../../api/workbench'
import { mapLayersLocation, readMapLayers, readWorkbenchSession, workbenchLocation } from './workbench-session'

const oldPlan: AnalysisPlan = {
  id: 'plan-a', version: 1, status: 'confirmed', task_id: 'task-a', goal: { summary: '旧目标' },
  method: { id: 'method-a', version: 1 }, inputs: {}, critical_options: {}, steps: [],
  expected_artifacts: [], check_rules: [], issues: [],
}
const newPlan: AnalysisPlan = {
  ...oldPlan, version: 2, status: 'draft', goal: { summary: '新目标' },
}
const run = {
  run_id: 'run-a', plan_id: 'plan-a', plan_version: 1,
  execution_status: 'SUCCEEDED', check_status: 'PASSED',
} as const
const task: TaskRecord = {
  task_id: 'task-a', goal: newPlan.goal, context: null,
  plans: [newPlan, oldPlan], runs: [run],
}

afterEach(() => vi.restoreAllMocks())

it('restores a run-only URL using its exact originating task and confirmed version', async () => {
  vi.spyOn(workbenchApi, 'runOrigin').mockResolvedValue({
    run_id: run.run_id, task_id: task.task_id, plan_id: run.plan_id, plan_version: run.plan_version,
  })
  const taskRequest = vi.spyOn(workbenchApi, 'task').mockResolvedValue(task)
  const planRequest = vi.spyOn(workbenchApi, 'plan').mockResolvedValue(oldPlan)
  const start = vi.spyOn(workbenchApi, 'start')
  const session = await readWorkbenchSession('?run=run-a')
  expect(taskRequest).toHaveBeenCalledWith(task.task_id, undefined)
  expect(planRequest).toHaveBeenCalledWith(oldPlan.id, oldPlan.version, undefined)
  expect(session).toEqual({ task, plan: oldPlan, run })
  expect(start).not.toHaveBeenCalled()
})

it('chooses the latest saved plan for a task-only URL and retains a separately specified run', async () => {
  vi.spyOn(workbenchApi, 'task').mockResolvedValue(task)
  vi.spyOn(workbenchApi, 'plan').mockResolvedValue(newPlan)
  vi.spyOn(workbenchApi, 'runOrigin').mockResolvedValue({
    run_id: run.run_id, task_id: task.task_id, plan_id: run.plan_id, plan_version: run.plan_version,
  })
  expect(await readWorkbenchSession('?task=task-a')).toEqual({ task, plan: newPlan, run: null })
  expect(await readWorkbenchSession('?task=task-a&plan=plan-a&version=2&run=run-a')).toEqual({ task, plan: newPlan, run })
})

it('rejects cross-task runs and foreign or incomplete plan links', async () => {
  vi.spyOn(workbenchApi, 'runOrigin').mockResolvedValue({
    run_id: run.run_id, task_id: 'task-b', plan_id: run.plan_id, plan_version: run.plan_version,
  })
  const taskRequest = vi.spyOn(workbenchApi, 'task').mockResolvedValue(task)
  await expect(readWorkbenchSession('?task=task-a&run=run-a')).rejects.toThrow('运行不属于此任务')
  expect(taskRequest).not.toHaveBeenCalled()
  await expect(readWorkbenchSession('?task=task-a&plan=foreign&version=1')).rejects.toThrow('方案版本不属于此任务')
  await expect(readWorkbenchSession('?task=task-a&plan=plan-a')).rejects.toThrow('方案链接缺少有效版本')
})

it('writes exact links and preserves unrelated search entries', () => {
  const base = new URL('http://localhost/?tab=preview&run=old#main')
  const next = workbenchLocation(base, { taskId: 'task-a', plan: oldPlan, runId: 'run-a' })
  expect(next.searchParams.get('tab')).toBe('preview')
  expect(next.searchParams.get('version')).toBe('1')
  expect(next.searchParams.get('run')).toBe('run-a')
  expect(next.hash).toBe('#main')
  expect(workbenchLocation(next, {}).search).toBe('?tab=preview')
})

it('round trips exact visible asset versions independently of the active plan and run', () => {
  const refs = [{ asset_id: 'asset@local', asset_version: 2 }, { asset_id: 'other', asset_version: 1 }]
  const url = mapLayersLocation(new URL('http://localhost/?run=run-a&layer=stale%401'), refs)
  assertLayers(url)
  url.searchParams.append('layer', 'invalid@0')
  url.searchParams.append('layer', 'other@1')
  assertLayers(url)
  expect(url.searchParams.get('run')).toBe('run-a')

  function assertLayers(location: URL) {
    expect(readMapLayers(location.search)).toEqual(refs)
  }
})
