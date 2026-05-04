import { useState } from "react"
import { useQuery } from "@tanstack/react-query"
import { Link } from "react-router-dom"
import {
  BarChart, Bar, Cell, PieChart, Pie, Tooltip,
  ResponsiveContainer, XAxis, YAxis, CartesianGrid,
} from "recharts"
import { CheckCircle2, XCircle, Clock, RefreshCw } from "lucide-react"

import { endpoints } from "@/api/endpoints"
import type { Period } from "@/api/types"
import { durationMs, fmtDuration, fmtRelative } from "@/lib/time"
import { KpiCard } from "@/components/KpiCard"
import { PeriodSelector } from "@/components/PeriodSelector"
import { StatusBadge } from "@/components/StatusBadge"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { Skeleton } from "@/components/ui/skeleton"
import {
  Table, TableBody, TableCell, TableHead, TableHeader, TableRow,
} from "@/components/ui/table"

/* ── helpers ───────────────────────────────────────────────────────────── */

function fmtCost(usd: number | null): string {
  if (usd == null) return "—"
  return `$${usd.toFixed(4)}`
}

function fmtTokens(n: number | null): string {
  if (n == null) return "—"
  return n >= 1_000 ? `${(n / 1_000).toFixed(1)}k` : String(n)
}

function fmtRate(r: number | null): string {
  if (r == null) return "—"
  return `${(r * 100).toFixed(1)}%`
}


const CHART_COLORS: Record<string, string> = {
  success: "#22c55e",
  failed:  "#ef4444",
  running: "#38bdf8",
  pending: "#52525b",
  error:   "#ef4444",
}

/* ── component ─────────────────────────────────────────────────────────── */

export function DashboardHome() {
  const [period, setPeriod] = useState<Period>("7d")

  const qSummary = useQuery({
    queryKey: ["metrics-summary", period],
    queryFn: () => endpoints.metricsSummary(period).then((r) => r.data),
    staleTime: 60_000,
  })

  const qCost = useQuery({
    queryKey: ["cost-summary", period],
    queryFn: () => endpoints.costSummary(period).then((r) => r.data),
    staleTime: 60_000,
  })

  const qApiHealth = useQuery({
    queryKey: ["api-health", period],
    queryFn: () => endpoints.apiHealth(period).then((r) => r.data),
    staleTime: 60_000,
  })

  const qTokenMonitor = useQuery({
    queryKey: ["token-monitor", period],
    queryFn: () => endpoints.tokenMonitor(period).then((r) => r.data),
    staleTime: 60_000,
  })

  const qExecutions = useQuery({
    queryKey: ["executions-recent"],
    queryFn: () => endpoints.executions({ limit: 5 }),
    staleTime: 30_000,
  })

  const summary   = qSummary.data
  const cost      = qCost.data
  const apiHealth = qApiHealth.data
  const token     = qTokenMonitor.data
  const execs     = qExecutions.data?.data ?? []

  /* Status distribution for pie chart */
  const statusData = summary
    ? Object.entries(summary.status_counts).map(([name, value]) => ({ name, value }))
    : []

  /* API health table rows */
  const serviceRows = apiHealth
    ? Object.entries(apiHealth.by_service).sort((a, b) => b[1].total_calls - a[1].total_calls)
    : []

  return (
    <div className="space-y-6">
      {/* Page header */}
      <div className="flex items-center justify-between">
        <h1 className="text-lg font-semibold text-foreground">ダッシュボード</h1>
        <PeriodSelector value={period} onChange={setPeriod} />
      </div>

      {/* KPI row */}
      <div className="grid grid-cols-2 lg:grid-cols-4 gap-4">
        <KpiCard
          title="実行回数"
          value={summary ? String(summary.total_executions) : null}
          sub={summary
            ? `成功 ${summary.status_counts.success ?? 0} / 失敗 ${summary.status_counts.failed ?? 0}`
            : undefined}
          loading={qSummary.isLoading}
        />
        <KpiCard
          title="成功率"
          value={summary
            ? fmtRate((summary.status_counts.success ?? 0) / Math.max(summary.total_executions, 1))
            : null}
          sub={summary ? `${period} 集計` : undefined}
          loading={qSummary.isLoading}
        />
        <KpiCard
          title="平均実行時間"
          value={summary ? fmtDuration(summary.avg_duration_ms) : null}
          sub="1実行あたり"
          loading={qSummary.isLoading}
        />
        <KpiCard
          title="実行あたりコスト"
          value={cost && cost.total_executions > 0
            ? fmtCost(cost.total_cost_usd != null ? cost.total_cost_usd / cost.total_executions : null)
            : cost ? "—" : null}
          sub={cost ? `期間合計: ${fmtCost(cost.total_cost_usd)}` : undefined}
          loading={qCost.isLoading}
        />
      </div>

      {/* Charts row */}
      <div className="grid grid-cols-1 lg:grid-cols-5 gap-4">
        {/* Status distribution */}
        <Card className="lg:col-span-3 bg-card border-border">
          <CardHeader className="pb-2">
            <CardTitle className="text-sm font-medium text-muted-foreground">実行結果の内訳</CardTitle>
          </CardHeader>
          <CardContent>
            {qSummary.isLoading ? (
              <Skeleton className="h-[180px] w-full" />
            ) : statusData.length === 0 ? (
              <EmptyState message="この期間のデータなし" />
            ) : (
              <ResponsiveContainer width="100%" height={180}>
                <BarChart data={statusData} margin={{ top: 0, right: 0, left: -20, bottom: 0 }}>
                  <CartesianGrid strokeDasharray="3 3" stroke="#2a2a2a" />
                  <XAxis dataKey="name" tick={{ fontSize: 11, fill: "#888" }} axisLine={false} tickLine={false} />
                  <YAxis tick={{ fontSize: 11, fill: "#888" }} axisLine={false} tickLine={false} />
                  <Tooltip
                    contentStyle={{ background: "#1a1a1a", border: "1px solid #2a2a2a", borderRadius: 6 }}
                    labelStyle={{ color: "#e8e8e8" }}
                    itemStyle={{ color: "#e8e8e8" }}
                  />
                  <Bar dataKey="value" radius={[4, 4, 0, 0]}>
                    {statusData.map((entry) => (
                      <Cell key={entry.name} fill={CHART_COLORS[entry.name] ?? "#6366f1"} />
                    ))}
                  </Bar>
                </BarChart>
              </ResponsiveContainer>
            )}
          </CardContent>
        </Card>

        {/* Review pass rate */}
        <Card className="lg:col-span-2 bg-card border-border">
          <CardHeader className="pb-2">
            <CardTitle className="text-sm font-medium text-muted-foreground">レビュー通過率</CardTitle>
          </CardHeader>
          <CardContent className="flex flex-col items-center justify-center h-[180px]">
            {qSummary.isLoading ? (
              <Skeleton className="h-24 w-24 rounded-full" />
            ) : summary?.review_pass_rate == null ? (
              <EmptyState message="レビューデータなし" />
            ) : (
              <>
                <ResponsiveContainer width="100%" height={140}>
                  <PieChart>
                    <Pie
                      data={[
                        { name: "通過", value: summary.review_pass_rate },
                        { name: "未通過", value: 1 - summary.review_pass_rate },
                      ]}
                      cx="50%"
                      cy="50%"
                      innerRadius={45}
                      outerRadius={62}
                      startAngle={90}
                      endAngle={-270}
                      paddingAngle={2}
                      dataKey="value"
                    >
                      <Cell fill="#22c55e" />
                      <Cell fill="#2a2a2a" />
                    </Pie>
                    <Tooltip
                      formatter={(v) => [`${(Number(v) * 100).toFixed(1)}%`]}
                      contentStyle={{ background: "#1a1a1a", border: "1px solid #2a2a2a", borderRadius: 6 }}
                    />
                  </PieChart>
                </ResponsiveContainer>
                <div className="text-2xl font-semibold tabular -mt-12">
                  {fmtRate(summary.review_pass_rate)}
                </div>
              </>
            )}
          </CardContent>
        </Card>
      </div>

      {/* Health row */}
      <div className="grid grid-cols-1 lg:grid-cols-5 gap-4">
        {/* API Health */}
        <Card className="lg:col-span-3 bg-card border-border">
          <CardHeader className="pb-2">
            <CardTitle className="text-sm font-medium text-muted-foreground">API 健全性</CardTitle>
          </CardHeader>
          <CardContent className="p-0">
            {qApiHealth.isLoading ? (
              <div className="p-4 space-y-2">
                {[...Array(4)].map((_, i) => <Skeleton key={i} className="h-8 w-full" />)}
              </div>
            ) : serviceRows.length === 0 ? (
              <div className="p-4"><EmptyState message="この期間の API 呼び出しなし" /></div>
            ) : (
              <Table>
                <TableHeader>
                  <TableRow className="border-border hover:bg-transparent">
                    <TableHead className="text-xs text-muted-foreground">サービス</TableHead>
                    <TableHead className="text-xs text-muted-foreground text-right">呼び出し</TableHead>
                    <TableHead className="text-xs text-muted-foreground text-right">成功率</TableHead>
                    <TableHead className="text-xs text-muted-foreground text-right">平均遅延</TableHead>
                    <TableHead className="text-xs text-muted-foreground text-right">レート制限</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {serviceRows.map(([svc, s]) => (
                    <TableRow key={svc} className="border-border">
                      <TableCell className="font-mono text-xs py-2">{svc}</TableCell>
                      <TableCell className="text-right text-xs tabular py-2">{s.total_calls}</TableCell>
                      <TableCell className="text-right text-xs tabular py-2">
                        <span className={s.success_rate != null && s.success_rate < 0.9 ? "text-red-400" : "text-green-400"}>
                          {fmtRate(s.success_rate)}
                        </span>
                      </TableCell>
                      <TableCell className="text-right text-xs tabular py-2">{s.avg_duration_ms != null ? `${s.avg_duration_ms}ms` : "—"}</TableCell>
                      <TableCell className="text-right text-xs tabular py-2">
                        <span className={s.rate_limit_count > 0 ? "text-amber-400" : "text-muted-foreground"}>
                          {s.rate_limit_count}
                        </span>
                      </TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            )}
          </CardContent>
        </Card>

        {/* Token Monitor */}
        <Card className="lg:col-span-2 bg-card border-border">
          <CardHeader className="pb-2">
            <CardTitle className="text-sm font-medium text-muted-foreground">Token Monitor</CardTitle>
          </CardHeader>
          <CardContent className="space-y-3">
            {qTokenMonitor.isLoading ? (
              <div className="space-y-2">{[...Array(4)].map((_, i) => <Skeleton key={i} className="h-5 w-full" />)}</div>
            ) : !token ? (
              <EmptyState message="データなし" />
            ) : (
              <>
                <TokenMonitorRow
                  icon={<CheckCircle2 size={14} className="text-green-400" />}
                  label="成功"
                  value={`${token.success_count} 回`}
                />
                <TokenMonitorRow
                  icon={<XCircle size={14} className="text-red-400" />}
                  label="失敗"
                  value={`${token.failure_count} 回`}
                />
                <TokenMonitorRow
                  icon={<RefreshCw size={14} className="text-sky-400" />}
                  label="最終更新"
                  value={fmtRelative(token.last_refresh_at)}
                />
                {token.last_failure_reason && (
                  <TokenMonitorRow
                    icon={<Clock size={14} className="text-amber-400" />}
                    label="最終エラー"
                    value={token.last_failure_reason}
                  />
                )}
                <div className="pt-1 border-t border-border">
                  <div className="flex justify-between items-center">
                    <span className="text-xs text-muted-foreground">成功率</span>
                    <span className={`text-sm font-semibold tabular ${
                      token.success_rate != null && token.success_rate < 0.9 ? "text-amber-400" : "text-green-400"
                    }`}>
                      {fmtRate(token.success_rate)}
                    </span>
                  </div>
                </div>
              </>
            )}
          </CardContent>
        </Card>
      </div>

      {/* Recent executions */}
      <Card className="bg-card border-border">
        <CardHeader className="pb-2">
          <div className="flex items-center justify-between">
            <CardTitle className="text-sm font-medium text-muted-foreground">最近の実行</CardTitle>
            <Link to="/executions" className="text-xs text-primary hover:underline">
              すべて表示 →
            </Link>
          </div>
        </CardHeader>
        <CardContent className="p-0">
          {qExecutions.isLoading ? (
            <div className="p-4 space-y-2">{[...Array(5)].map((_, i) => <Skeleton key={i} className="h-10 w-full" />)}</div>
          ) : execs.length === 0 ? (
            <div className="p-4"><EmptyState message="実行履歴なし" /></div>
          ) : (
            <Table>
              <TableHeader>
                <TableRow className="border-border hover:bg-transparent">
                  <TableHead className="text-xs text-muted-foreground">状態</TableHead>
                  <TableHead className="text-xs text-muted-foreground">ID</TableHead>
                  <TableHead className="text-xs text-muted-foreground">トピック</TableHead>
                  <TableHead className="text-xs text-muted-foreground text-right">実行時間</TableHead>
                  <TableHead className="text-xs text-muted-foreground text-right">トークン</TableHead>
                  <TableHead className="text-xs text-muted-foreground text-right">コスト</TableHead>
                  <TableHead className="text-xs text-muted-foreground text-right">開始日時</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {execs.map((ex) => (
                  <TableRow key={ex.execution_id} className="border-border cursor-pointer hover:bg-accent/50">
                    <TableCell className="py-2.5">
                      <StatusBadge status={ex.status} />
                    </TableCell>
                    <TableCell className="py-2.5">
                      <Link
                        to={`/executions/${ex.execution_id}`}
                        className="font-mono text-xs text-primary hover:underline"
                      >
                        {ex.execution_id.slice(0, 12)}…
                      </Link>
                    </TableCell>
                    <TableCell className="py-2.5 max-w-[200px]">
                      <span className="text-xs text-foreground line-clamp-1">{ex.topic}</span>
                    </TableCell>
                    <TableCell className="py-2.5 text-right text-xs tabular text-muted-foreground">
                      {fmtDuration(durationMs(ex.created_at, ex.completed_at))}
                    </TableCell>
                    <TableCell className="py-2.5 text-right text-xs tabular text-muted-foreground">
                      {fmtTokens(ex.total_tokens_used ?? null)}
                    </TableCell>
                    <TableCell className="py-2.5 text-right text-xs tabular text-muted-foreground">
                      {fmtCost(ex.total_cost_usd ?? null)}
                    </TableCell>
                    <TableCell className="py-2.5 text-right text-xs tabular text-muted-foreground">
                      {fmtRelative(ex.created_at)}
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

/* ── sub-components ─────────────────────────────────────────────────────── */

function EmptyState({ message }: { message: string }) {
  return (
    <div className="flex items-center justify-center h-16 text-xs text-muted-foreground">
      {message}
    </div>
  )
}

function TokenMonitorRow({
  icon, label, value,
}: { icon: React.ReactNode; label: string; value: string }) {
  return (
    <div className="flex items-center gap-2">
      {icon}
      <span className="text-xs text-muted-foreground flex-1">{label}</span>
      <span className="text-xs tabular text-foreground">{value}</span>
    </div>
  )
}
