import type { MethodRef, TaskRecord } from '../../api/workbench'
import { workbenchLocation } from './workbench-session'
import './session.css'

const planStatus = { draft: '草稿', confirmed: '已确认' } as const
const executionStatus = {
  NOT_STARTED: '排队中', RUNNING: '运行中', SUCCEEDED: '成功', FAILED: '失败', CANCELLED: '已取消',
} as const

export function TaskHistory({ task, currentPlan, currentRunId }: {
  task: TaskRecord
  currentPlan: MethodRef | null
  currentRunId: string | null
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
      {task.runs.length > 0 && (
        <ul aria-label="运行历史">
          {task.runs.map((run) => {
            const url = workbenchLocation(location, { taskId: task.task_id, runId: run.run_id })
            return (
              <li key={run.run_id}>
                <a href={url.href} aria-current={run.run_id === currentRunId || undefined}>{run.run_id.slice(0, 8)}</a>
                <span>{executionStatus[run.execution_status]}</span>
              </li>
            )
          })}
        </ul>
      )}
    </details>
  )
}
