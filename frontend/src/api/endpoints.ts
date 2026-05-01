import { api } from "./client"
import type {
  ApiHealth,
  CostSummary,
  ExecutionListResponse,
  MetricsSummary,
  Period,
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

  executions: (limit = 5) =>
    api.get<ExecutionListResponse>(`/api/v1/executions?limit=${limit}`),
}
