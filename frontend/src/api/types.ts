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

export type Period = "24h" | "7d" | "30d"
