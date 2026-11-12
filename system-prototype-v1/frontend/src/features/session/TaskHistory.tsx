import type { MethodRef, TaskRecord } from '../../api/workbench'
import { workbenchLocation } from './workbench-session'
import './session.css'

const planStatus = { draft: '草稿', confirmed: '已确认' } as const
const executionStatus = {
  NOT_STARTED: '排队中', RUNNING: '运行中', SUCCEEDED: '执行成功', FAILED: '执行失败', CANCELLED: '已取消',
} as const
const checkStatus = {
  NOT_CHECKED: '未检查', CHECKING: '检查中', PASSED: '检查通过', WARNING: '检查有警告', FAILED: '检查未通过',
} as const

export function TaskHistory({ task, currentPlan }: {
  task: TaskRecord
  currentPlan: MethodRef | null
}) {
  const location = new URL(window.location.href)
  return (
    <details className="task-history">
      <summary>任务记录</summary>
      <p>任务编号 <code>{task.task_id}</code></p>
      {task.plans.length > 0 && (
        <ul aria-label="方案历史">
          {task.plans.map((plan) => {
            const isCurrent = currentPlan?.id === plan.id && currentPlan.version === plan.version
            const url = workbenchLocation(location, { taskId: task.task_id, plan })
            return (
              <li key={`${plan.id}@${plan.version}`}>
                <a href={url.href} aria-current={isCurrent || undefined}>{plan.id.slice(0, 8)} · v{plan.version}</a>
                <span>{planStatus[plan.status]}</span>
              </li>
            )
          })}
        </ul>
      )}
    </details>
  )
}

export function RunHistory({ task, currentRunId }: { task: TaskRecord; currentRunId: string | null }) {
  if (!task.runs.length) return null
  const location = new URL(window.location.href)
  return (
    <details className="task-history">
      <summary>运行历史（{task.runs.length} 次）</summary>
      <ul aria-label="运行历史">
        {task.runs.map((run) => {
          const url = workbenchLocation(location, { taskId: task.task_id, runId: run.run_id })
          return (
            <li key={run.run_id}>
              <a href={url.href} aria-current={run.run_id === currentRunId || undefined} title={run.run_id}>
                {run.run_id.slice(0, 8)} · 方案 v{run.plan_version}
              </a>
              <span>{executionStatus[run.execution_status]}</span>
              {run.execution_status === 'SUCCEEDED' && <span>{checkStatus[run.check_status]}</span>}
            </li>
          )
        })}
      </ul>
    </details>
  )
}
