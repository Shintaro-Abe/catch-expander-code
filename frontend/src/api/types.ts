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
  skip_count: number
  success_rate: number | null
  last_refresh_at: string | null
  last_failure_at: string | null
  last_skip_at: string | null
  last_check_at: string | null
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
  total_tokens_used?: number | null
  total_input_tokens?: number | null
  total_output_tokens?: number | null
  total_cost_usd?: number | null
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

export interface AuthMeResponse {
  user_sub: string
  user_name: string
  expires_at: number
}

// 20260706-preference-scope: 学習済み好みの適用スコープ。
// 両リスト空 = 汎用（全プロンプト注入対象）。categories はトピックカテゴリ 5 値、
// deliverables は成果物区分 6 値 (code は iac_code + program_code に展開される)。
export interface LearnedPreference {
  text: string
  scope: {
    categories: string[]
    deliverables: string[]
  }
}

// F6 User Profile 閲覧用: dashboard_api/get_my_profile が返す本人プロファイル。
// 値が REMOVE 済 / 未設定の軸は null で返る。learned_preferences は要素なしなら空配列。
export interface MyProfile {
  user_id: string
  role: string | null
  interests: string | null
  expertise: string | null
  learning_goals: string | null
  background: string | null
  output_preferences: string | null
  learned_preferences: LearnedPreference[]
  updated_at: string | null
}

export interface MyProfileResponse {
  data: MyProfile
}

export type Period = "24h" | "7d" | "30d"

export interface FeedbackEvent {
  execution_id: string
  timestamp: string
  subtype: string
  reply_text_summary: string
  learned_preferences_updated: boolean
  new_preferences_count: number
  total_preferences_count: number | null
}

export interface DailyCount {
  date: string
  count: number
}

export interface FeedbackAggregation {
  period: string
  total_feedback_count: number
  preferences_updated_count: number
  avg_new_preferences: number | null
  latest_total_preferences: number | null
  daily_counts?: DailyCount[]
  events?: FeedbackEvent[]
}

export interface FeedbackAggregationResponse {
  data: FeedbackAggregation
}

// 2026-05-13: text generator workspace 化に伴い subagent literal を拡張
// (.steering/20260512-parse-claude-response-dict-contract/)
// - "generator_text": 新方式 (Write tool で deliverable.json に書く)
// - "generator_code": 新方式 (code 成果物別パス)
// - "generator": 後方互換 (旧 record / feature flag false 時)
export interface SubagentIORecord {
  subagent:
    | "researcher"
    | "generator"
    | "generator_text"
    | "generator_code"
    | "reviewer_eval"
    | "reviewer_fix"
  index: string
  prompt: string
  output: string
  output_files?: Record<string, string>
  recorded_at: string
}

export interface SubagentIOResponse {
  data: {
    execution_id: string
    records: SubagentIORecord[]
  }
}
