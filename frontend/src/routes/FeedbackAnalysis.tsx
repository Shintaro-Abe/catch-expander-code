import { useState } from "react"
import { useQuery } from "@tanstack/react-query"
import { MessageSquare } from "lucide-react"

import { endpoints } from "@/api/endpoints"
import type { Period } from "@/api/types"
import { KpiCard } from "@/components/KpiCard"
import { PeriodSelector } from "@/components/PeriodSelector"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { Skeleton } from "@/components/ui/skeleton"

export function FeedbackAnalysis() {
  const [period, setPeriod] = useState<Period>("7d")

  const q = useQuery({
    queryKey: ["feedbackAggregation", period],
    queryFn: () => endpoints.feedbackAggregation(period),
    staleTime: 60_000,
  })

  const d = q.data?.data

  const updateRate =
    d && d.total_feedback_count > 0
      ? `${((d.preferences_updated_count / d.total_feedback_count) * 100).toFixed(1)}%`
      : null

  return (
    <div className="space-y-4">
      {/* Header */}
      <div className="flex items-center justify-between">
        <h1 className="text-lg font-semibold text-foreground">フィードバック分析</h1>
        <PeriodSelector value={period} onChange={setPeriod} />
      </div>

      {/* KPI row */}
      <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4">
        <KpiCard
          title="総フィードバック数"
          value={d ? String(d.total_feedback_count) : null}
          loading={q.isLoading}
        />
        <KpiCard
          title="設定更新件数"
          value={d ? String(d.preferences_updated_count) : null}
          sub={updateRate ? `更新率 ${updateRate}` : undefined}
          loading={q.isLoading}
        />
        <KpiCard
          title="平均新規設定数"
          value={d?.avg_new_preferences != null ? String(d.avg_new_preferences) : null}
          loading={q.isLoading}
        />
        <KpiCard
          title="累計設定数"
          value={d?.latest_total_preferences != null ? String(d.latest_total_preferences) : null}
          loading={q.isLoading}
        />
      </div>

      {/* Detail card */}
      <Card className="bg-card border-border">
        <CardHeader className="px-4 pt-4 pb-2">
          <div className="flex items-center gap-2">
            <MessageSquare size={13} className="text-primary" />
            <CardTitle className="text-sm font-semibold text-foreground">サマリ</CardTitle>
          </div>
        </CardHeader>
        <CardContent className="px-4 pb-4">
          {q.isLoading ? (
            <div className="space-y-2">
              <Skeleton className="h-4 w-2/3" />
              <Skeleton className="h-4 w-1/2" />
            </div>
          ) : q.isError ? (
            <p className="text-xs text-destructive">データの取得に失敗しました</p>
          ) : d && d.total_feedback_count === 0 ? (
            <p className="text-xs text-muted-foreground">
              この期間にフィードバックイベントはありません
            </p>
          ) : d ? (
            <dl className="grid grid-cols-1 sm:grid-cols-2 gap-x-8 gap-y-2 text-xs">
              <div className="flex justify-between">
                <dt className="text-muted-foreground">期間</dt>
                <dd className="tabular text-foreground">{d.period}</dd>
              </div>
              <div className="flex justify-between">
                <dt className="text-muted-foreground">フィードバック数</dt>
                <dd className="tabular text-foreground">{d.total_feedback_count}</dd>
              </div>
              <div className="flex justify-between">
                <dt className="text-muted-foreground">設定更新件数</dt>
                <dd className="tabular text-foreground">{d.preferences_updated_count}</dd>
              </div>
              <div className="flex justify-between">
                <dt className="text-muted-foreground">更新率</dt>
                <dd className="tabular text-foreground">{updateRate ?? "—"}</dd>
              </div>
              <div className="flex justify-between">
                <dt className="text-muted-foreground">平均新規設定数</dt>
                <dd className="tabular text-foreground">
                  {d.avg_new_preferences != null ? d.avg_new_preferences : "—"}
                </dd>
              </div>
              <div className="flex justify-between">
                <dt className="text-muted-foreground">累計設定数（最新スナップショット）</dt>
                <dd className="tabular text-foreground">
                  {d.latest_total_preferences != null ? d.latest_total_preferences : "—"}
                </dd>
              </div>
            </dl>
          ) : null}
        </CardContent>
      </Card>
    </div>
  )
}
