import { useId, type ReactNode } from 'react'

export type ExecutionStatus = 'NOT_STARTED' | 'RUNNING' | 'SUCCEEDED' | 'FAILED' | 'CANCELLED'
export type CheckStatus = 'NOT_CHECKED' | 'CHECKING' | 'PASSED' | 'WARNING' | 'FAILED'

export interface WorkbenchPageProps {
  assets?: ReactNode
  map?: ReactNode
  task?: ReactNode
  plan?: ReactNode
  run?: ReactNode
  artifacts?: ReactNode
  checks?: ReactNode
  scriptRunsHref?: string
  executionStatus?: ExecutionStatus
  checkStatus?: CheckStatus
}

const executionLabels = {
  NOT_STARTED: ['排队中', 'neutral'],
  RUNNING: ['运行中', 'active'],
  SUCCEEDED: ['运行成功', 'success'],
  FAILED: ['运行失败', 'error'],
  CANCELLED: ['已取消', 'neutral'],
} as const

const checkLabels = {
  NOT_CHECKED: ['规则检查未完成', 'neutral'],
  CHECKING: ['规则检查中', 'active'],
  PASSED: ['规则检查通过', 'success'],
  WARNING: ['规则检查有警告', 'warning'],
  FAILED: ['规则检查未通过', 'error'],
} as const

function Region({ title, className = '', children }: { title: string; className?: string; children: ReactNode }) {
  const titleId = useId()
  return (
    <section className={`workbench__region ${className}`} aria-labelledby={titleId}>
      <h2 id={titleId}>{title}</h2>
      {children}
    </section>
  )
}

export function WorkbenchPage({
  assets, map, task, plan, run, artifacts, checks, scriptRunsHref, executionStatus, checkStatus,
}: WorkbenchPageProps) {
  const checkTitleId = useId()
  const [executionLabel, executionTone] = executionStatus ? executionLabels[executionStatus] : ['尚未运行', 'neutral']
  const [checkLabel, checkTone] = checkStatus ? checkLabels[checkStatus] : ['尚未检查', 'neutral']

  return (
    <main className="workbench">
      <header className="workbench__header workbench__header--analysis">
        <h1>GeoPilot <span>工作台</span></h1>
        {scriptRunsHref && <a href={scriptRunsHref}>脚本运行</a>}
      </header>

      <div className="workbench__surface">
        <Region title="数据" className="workbench__data">
          {assets ?? (
            <div className="workbench__data-empty">
              <p className="workbench__placeholder">暂无数据</p>
              <button className="workbench__action" type="button" disabled>导入数据</button>
            </div>
          )}
        </Region>

        <Region title="地图" className="workbench__map">
          <div className="workbench__map-content">
            {map ?? (
              <div className="workbench__map-empty">
                <p className="workbench__placeholder">暂无图层</p>
              </div>
            )}
          </div>
        </Region>

        <div className="workbench__workflow">
          <Region title="任务">
            {task ?? <p className="workbench__placeholder">尚未选择任务</p>}
          </Region>
          <Region title="方案">
            {plan ?? (
              <>
                <p className="workbench__placeholder">尚无方案</p>
                <button className="workbench__action" type="button" disabled>确认方案</button>
              </>
            )}
          </Region>
          <Region title="运行">
            <span className="workbench__status" role="status" data-tone={executionTone}>{executionLabel}</span>
            {run ?? <button className="workbench__action" type="button" disabled>开始运行</button>}
          </Region>
          <Region title="结果">
            {artifacts ?? <p className="workbench__placeholder">暂无成果</p>}
          </Region>
        </div>
      </div>

      <section className="workbench__checks" aria-labelledby={checkTitleId}>
        <details>
          <summary>
            <span id={checkTitleId} className="workbench__checks-title">规则检查</span>
            <span className="workbench__status" role="status" data-tone={checkTone}>{checkLabel}</span>
          </summary>
          <div className="workbench__checks-content">
            {checks ?? <p className="workbench__placeholder">暂无检查报告</p>}
          </div>
        </details>
      </section>
    </main>
  )
}
