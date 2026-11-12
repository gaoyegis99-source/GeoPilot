import type { AnalysisPlan, ContextCriteria, Json } from './workbench'

export interface PlanComparison {
  plan: AnalysisPlan
  criteria: ContextCriteria
  input_types: Record<string, string>
  option_specs: Record<string, Json>
  preconditions: string[]
  measurement: Record<string, Json>
}

export interface PlanAlternatives {
  baseline: PlanComparison
  alternatives: PlanComparison[]
}
