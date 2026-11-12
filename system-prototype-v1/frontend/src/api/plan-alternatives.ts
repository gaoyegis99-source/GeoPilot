import type { AnalysisPlan, CheckStatus, ContextCriteria, ExecutionStatus, Json } from './workbench'

export interface PlanComparison {
  plan: AnalysisPlan
  criteria: ContextCriteria
  input_types: Record<string, string>
  option_specs: Record<string, Json>
  preconditions: string[]
  measurement: Record<string, Json>
}

export interface PlanAlternatives<T extends PlanComparison = PlanComparison> {
  baseline: PlanComparison
  alternatives: T[]
}

export interface FailureProposal extends PlanComparison {
  proposal_id: string
}

export interface FailureReplanning extends PlanAlternatives<FailureProposal> {
  run_id: string
  execution_status: ExecutionStatus
  check_status: CheckStatus
}
