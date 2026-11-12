import { useCallback } from 'react'
import { type MethodRef, workbenchApi } from '../../api/workbench'
import { AlternativePlansPanel } from '../plans/AlternativePlansPanel'

export function FailureReplanPanel({ runId, plan, disabled, onApply }: {
  runId: string
  plan: MethodRef
  disabled: boolean
  onApply: (proposalId: string) => void
}) {
  const load = useCallback(async (_id: string, _version: number, signal: AbortSignal) => {
    const result = await workbenchApi.replanning(runId, signal)
    if (result.run_id !== runId) throw new Error('Unexpected run revision reference')
    return result
  }, [runId])
  return <AlternativePlansPanel
    plan={plan} load={load} disabled={disabled}
    title="比较此失败运行的修订方案"
    emptyMessage="当前没有可用的修订建议。可以打开原方案编辑输入或参数。"
    onSelect={choice => onApply(choice.proposal_id)}
  />
}
