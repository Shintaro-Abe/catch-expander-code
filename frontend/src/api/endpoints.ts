import { api } from "./client"
import type {
  ApiHealth,
  CostSummary,
  ErrorListResponse,
  ExecutionDetailResponse,
  ExecutionEventsResponse,
  ExecutionListResponse,
  MetricsSummary,
  Period,
  ReviewQualityResponse,
  TokenMonitorHealth,
} from "./types"

export const endpoints = {
  metricsSummary: (period: Period) =>
    api.get<{ data: MetricsSummary }>(`/api/v1/metrics/summary?period=${period}`),

  costSummary: (period: Period) =>
    api.get<{ data: CostSummary }>(`/api/v1/metrics/cost?period=${period}`),

  apiHealth: (period: Period) =>
    api.get<{ data: ApiHealth }>(`/api/v1/metrics/api-health?period=${period}`),

  tokenMonitor: (period: Period) =>
    api.get<{ data: TokenMonitorHealth }>(`/api/v1/metrics/token-monitor?period=${period}`),

  executions: (params?: { limit?: number; from?: string; to?: string; status?: string; topic?: string }) => {
    const q = new URLSearchParams()
    if (params?.limit)  q.set("limit",  String(params.limit))
    if (params?.from)   q.set("from",   params.from)
    if (params?.to)     q.set("to",     params.to)
    if (params?.status) q.set("status", params.status)
    if (params?.topic)  q.set("topic",  params.topic)
    return api.get<ExecutionListResponse>(`/api/v1/executions?${q}`)
  },

  reviewQuality: (days: number) =>
    api.get<ReviewQualityResponse>(`/api/v1/review-quality?days=${days}`),

  errors: (days: number) =>
    api.get<ErrorListResponse>(`/api/v1/errors?days=${days}`),

  execution: (id: string) =>
    api.get<ExecutionDetailResponse>(`/api/v1/executions/${id}`),

  executionEvents: (id: string) =>
    api.get<ExecutionEventsResponse>(`/api/v1/executions/${id}/events`),
}
