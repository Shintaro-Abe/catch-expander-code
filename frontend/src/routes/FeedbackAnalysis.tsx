import { useState } from "react"
import { useQuery } from "@tanstack/react-query"
import { Link } from "react-router-dom"
import {
  LineChart, Line, XAxis, YAxis, CartesianGrid, Tooltip,
  PieChart, Pie, Cell,
  ResponsiveContainer,
} from "recharts"
import { MessageSquare, CheckCircle2, XCircle, Clock } from "lucide-react"

import { endpoints } from "@/api/endpoints"
import type { Period } from "@/api/types"
import { KpiCard } from "@/components/KpiCard"
import { PeriodSelector } from "@/components/PeriodSelector"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { Skeleton } from "@/components/ui/skeleton"
import {
  Table, TableBody, TableCell, TableHead, TableHeader, TableRow,
} from "@/components/ui/table"
import { fmtDatetime } from "@/lib/time"

const PIE_COLORS = { updated: "#22c55e", not_updated: "#2a2a2a" }

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

  const pieData = d && d.total_feedback_count > 0
    ? [
        { name: "設定更新あり", value: d.preferences_updated_count },
        { name: "設定更新なし", value: d.total_feedback_count - d.preferences_updated_count },
      ]
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

      {/* Trend chart */}
      <Card className="bg-card border-border">
        <CardHeader className="px-4 pt-4 pb-2">
          <div className="flex items-center gap-2">
            <MessageSquare size={13} className="text-primary" />
            <CardTitle className="text-sm font-semibold text-foreground">件数推移（日別）</CardTitle>
          </div>
        </CardHeader>
        <CardContent className="px-4 pb-4">
          {q.isLoading ? (
            <Skeleton className="h-[180px] w-full" />
          ) : q.isError ? (
            <p className="text-xs text-destructive">データの取得に失敗しました</p>
          ) : !d?.daily_counts?.length ? (
            <p className="text-xs text-muted-foreground py-8 text-center">
              この期間にフィードバックイベントはありません
            </p>
          ) : (
            <ResponsiveContainer width="100%" height={180}>
              <LineChart data={d.daily_counts}>
                <CartesianGrid strokeDasharray="3 3" stroke="#2a2a2a" />
                <XAxis dataKey="date" tick={{ fontSize: 11 }} stroke="#52525b" />
                <YAxis allowDecimals={false} tick={{ fontSize: 11 }} stroke="#52525b" />
                <Tooltip
                  contentStyle={{ background: "#1a1a1a", border: "1px solid #2a2a2a", borderRadius: 6 }}
                  labelStyle={{ color: "#e4e4e7" }}
                  itemStyle={{ color: "#38bdf8" }}
                />
                <Line
                  type="monotone"
                  dataKey="count"
                  name="件数"
                  stroke="#38bdf8"
                  strokeWidth={2}
                  dot={{ r: 3, fill: "#38bdf8" }}
                  activeDot={{ r: 5 }}
                />
              </LineChart>
            </ResponsiveContainer>
          )}
        </CardContent>
      </Card>

      {/* Pie + Emoji placeholder row */}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
        {/* Preferences update pie */}
        <Card className="bg-card border-border">
          <CardHeader className="px-4 pt-4 pb-2">
            <CardTitle className="text-sm font-semibold text-foreground">設定更新率</CardTitle>
          </CardHeader>
          <CardContent className="px-4 pb-4 flex flex-col items-center justify-center h-[200px]">
            {q.isLoading ? (
              <Skeleton className="h-24 w-24 rounded-full" />
            ) : !pieData ? (
              <p className="text-xs text-muted-foreground">データなし</p>
            ) : (
              <>
                <ResponsiveContainer width="100%" height={150}>
                  <PieChart>
                    <Pie
                      data={pieData}
                      cx="50%"
                      cy="50%"
                      innerRadius={45}
                      outerRadius={62}
                      startAngle={90}
                      endAngle={-270}
                      paddingAngle={2}
                      dataKey="value"
                    >
                      <Cell fill={PIE_COLORS.updated} />
                      <Cell fill={PIE_COLORS.not_updated} />
                    </Pie>
                    <Tooltip
                      formatter={(v, name) => [`${v}件`, name]}
                      contentStyle={{ background: "#1a1a1a", border: "1px solid #2a2a2a", borderRadius: 6 }}
                    />
                  </PieChart>
                </ResponsiveContainer>
                <div className="text-2xl font-semibold tabular -mt-12">
                  {updateRate ?? "—"}
                </div>
              </>
            )}
          </CardContent>
        </Card>

        {/* Emoji reactions placeholder */}
        <Card className="bg-card border-border">
          <CardHeader className="px-4 pt-4 pb-2">
            <CardTitle className="text-sm font-semibold text-foreground">絵文字反応の分布</CardTitle>
          </CardHeader>
          <CardContent className="px-4 pb-4 flex flex-col items-center justify-center h-[200px] gap-2">
            <Clock size={28} className="text-muted-foreground" />
            <p className="text-xs text-muted-foreground text-center">
              将来実装予定
            </p>
            <p className="text-xs text-muted-foreground text-center">
              絵文字反応（👍/👎）のデータは未収集です
            </p>
          </CardContent>
        </Card>
      </div>

      {/* Feedback history table */}
      <Card className="bg-card border-border">
        <CardHeader className="px-4 pt-4 pb-2">
          <div className="flex items-center gap-2">
            <MessageSquare size={13} className="text-primary" />
            <CardTitle className="text-sm font-semibold text-foreground">フィードバック履歴</CardTitle>
          </div>
        </CardHeader>
        <CardContent className="px-4 pb-4">
          {q.isLoading ? (
            <div className="space-y-2">
              {[...Array(3)].map((_, i) => <Skeleton key={i} className="h-8 w-full" />)}
            </div>
          ) : q.isError ? (
            <p className="text-xs text-destructive">データの取得に失敗しました</p>
          ) : !d?.events?.length ? (
            <p className="text-xs text-muted-foreground">
              この期間にフィードバックイベントはありません
            </p>
          ) : (
            <Table>
              <TableHeader>
                <TableRow className="border-border hover:bg-transparent">
                  <TableHead className="text-xs text-muted-foreground w-[130px]">受信日時</TableHead>
                  <TableHead className="text-xs text-muted-foreground w-[120px]">実行ID</TableHead>
                  <TableHead className="text-xs text-muted-foreground">フィードバック内容</TableHead>
                  <TableHead className="text-xs text-muted-foreground w-[72px] text-center">設定更新</TableHead>
                  <TableHead className="text-xs text-muted-foreground w-[72px] text-right">新規設定数</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {d.events.map((e, i) => (
                  <TableRow key={i} className="border-border">
                    <TableCell className="text-xs text-muted-foreground tabular whitespace-nowrap">
                      {fmtDatetime(e.timestamp)}
                    </TableCell>
                    <TableCell className="text-xs font-mono">
                      <Link
                        to={`/executions/${e.execution_id}`}
                        className="text-primary hover:underline"
                      >
                        {e.execution_id.slice(0, 12)}…
                      </Link>
                    </TableCell>
                    <TableCell className="text-xs text-foreground max-w-[320px] truncate">
                      {e.reply_text_summary || "—"}
                    </TableCell>
                    <TableCell className="text-center">
                      {e.learned_preferences_updated
                        ? <CheckCircle2 size={14} className="text-green-500 mx-auto" />
                        : <XCircle size={14} className="text-muted-foreground mx-auto" />
                      }
                    </TableCell>
                    <TableCell className="text-xs tabular text-right">
                      {e.new_preferences_count}
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          )}
        </CardContent>
      </Card>
    </div>
  )
}
