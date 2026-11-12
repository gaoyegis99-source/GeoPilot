import type { AnalysisPlan, MethodRef } from '../../api/workbench'

interface ActionProps {
  plan: (MethodRef & { status: AnalysisPlan['status']; issues: readonly unknown[] }) | null
  dirty?: boolean
  busy?: boolean
}

export function ConfirmPlanButton({ plan, dirty, busy, onConfirm }: ActionProps & {
  onConfirm: (version: MethodRef) => void
}) {
  const enabled = plan?.status === 'draft' && plan.issues.length === 0 && !dirty && !busy
  return (
    <button className="workbench__action" type="button" disabled={!enabled}
      onClick={() => { if (enabled) onConfirm({ id: plan.id, version: plan.version }) }}>
      确认方案
    </button>
  )
}

export function StartPlanButton({ plan, dirty, busy, onStart }: ActionProps & {
  onStart: (version: MethodRef) => void
}) {
  const enabled = plan?.status === 'confirmed' && !dirty && !busy
  return (
    <button className="workbench__action" type="button" disabled={!enabled}
      onClick={() => { if (enabled) onStart({ id: plan.id, version: plan.version }) }}>
      开始运行
    </button>
  )
}
