export interface MetricsSummary {
  period: string
  total_executions: number
  status_counts: Record<string, number>
  avg_duration_ms: number | null
  review_pass_rate: number | null
}

export interface CostSummary {
  period: string
  total_executions: number
  total_tokens_used: number | null
  total_cost_usd: number | null
  avg_tokens_per_execution: number | null
}

export interface ServiceHealth {
  total_calls: number
  success_rate: number | null
  rate_limit_count: number
  avg_duration_ms: number | null
}

export interface ApiHealth {
  period: string
  by_service: Record<string, ServiceHealth>
}

export interface TokenMonitorHealth {
  period: string
  total_refresh_attempts: number
  success_count: number
  failure_count: number
  success_rate: number | null
  last_refresh_at: string | null
  last_failure_at: string | null
  last_failure_reason: string | null
}

export interface Execution {
  execution_id: string
  user_id: string
  topic: string
  status: string
  created_at: string
  completed_at?: string
  category?: string
  slack_channel?: string
  workflow_plan?: Record<string, unknown>
  intent?: string
  perspectives?: string[]
  deliverable_types?: string[]
}

export interface ExecutionListResponse {
  data: Execution[]
  meta: { total: number; next_cursor: string | null }
}

export interface DashboardEvent {
  execution_id: string
  sk: string
  event_type: string
  timestamp: string
  sequence_number: number
  status_at_emit: string
  payload: Record<string, unknown>
}

export interface Deliverable {
  execution_id: string
  deliverable_id?: string
  deliverable_type?: string
  title?: string
  created_at?: string
  [key: string]: unknown
}

export interface ExecutionDetailResponse {
  data: {
    execution: Execution
    deliverables: Deliverable[]
  }
}

export interface ExecutionEventsResponse {
  data: DashboardEvent[]
  meta: { total: number }
}

export interface UnfixedCodeIssue {
  execution_id: string
  timestamp: string
  iteration: number | null
  code_related_unfixed_count: number
}

export interface ReviewQualityData {
  period_days: number
  total_reviews: number
  pass_count: number
  pass_rate: number | null
  unfixed_code_issues: UnfixedCodeIssue[]
}

export interface ReviewQualityResponse {
  data: ReviewQualityData
}

export interface ErrorItem {
  execution_id: string
  timestamp: string
  error_type: string
  error_message: string
  stage: string
  is_recoverable: boolean | null
}

export interface ErrorListResponse {
  data: ErrorItem[]
  meta: { total: number; by_type: Record<string, number> }
}

export type Period = "24h" | "7d" | "30d"
