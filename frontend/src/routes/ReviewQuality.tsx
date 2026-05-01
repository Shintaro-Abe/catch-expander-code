import { useState } from "react"
import { useQuery } from "@tanstack/react-query"
import { Link } from "react-router-dom"
import { AlertTriangle } from "lucide-react"

import { endpoints } from "@/api/endpoints"
import type { UnfixedCodeIssue } from "@/api/types"
import { KpiCard } from "@/components/KpiCard"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { Skeleton } from "@/components/ui/skeleton"
import {
  Table, TableBody, TableCell, TableHead, TableHeader, TableRow,
} from "@/components/ui/table"
import { fmtRelative, fmtDatetime } from "@/lib/time"

/* ── constants ─────────────────────────────────────────────────────────── */

const DAYS_OPTIONS = [
  { value: 7,  label: "7日"  },
  { value: 30, label: "30日" },
]

/* ── component ─────────────────────────────────────────────────────────── */

export function ReviewQuality() {
  const [days, setDays] = useState(30)

  const q = useQuery({
    queryKey: ["reviewQuality", days],
    queryFn: () => endpoints.reviewQuality(days),
    staleTime: 60_000,
  })

  const d      = q.data?.data
  const issues = [...(d?.unfixed_code_issues ?? [])].sort(
    (a, b) => b.code_related_unfixed_count - a.code_related_unfixed_count,
  )

  const passRateLabel = d?.pass_rate != null
    ? `${(d.pass_rate * 100).toFixed(1)}%`
    : null

  return (
    <div className="space-y-4">
      {/* Header */}
      <div className="flex items-center justify-between">
        <h1 className="text-lg font-semibold text-foreground">レビュー品質</h1>
        <DaysSelector value={days} onChange={(v) => setDays(v)} />
      </div>

      {/* KPI row */}
      <div className="grid grid-cols-1 sm:grid-cols-3 gap-4">
        <KpiCard
          title="総レビュー数"
          value={d ? String(d.total_reviews) : null}
          loading={q.isLoading}
        />
        <KpiCard
          title="合格数"
          value={d ? String(d.pass_count) : null}
          loading={q.isLoading}
        />
        <KpiCard
          title="合格率"
          value={passRateLabel}
          sub={d ? `${d.pass_count} / ${d.total_reviews}` : undefined}
          loading={q.isLoading}
        />
      </div>

      {/* Unfixed issues */}
      <Card className="bg-card border-border">
        <CardHeader className="px-4 pt-4 pb-2">
          <div className="flex items-center gap-2">
            <AlertTriangle size={13} className="text-orange-400" />
            <CardTitle className="text-sm font-semibold text-foreground">
              未修正コード問題
              {!q.isLoading && (
                <span className="ml-1.5 text-muted-foreground font-normal text-xs">
                  ({issues.length} 件)
                </span>
              )}
            </CardTitle>
          </div>
        </CardHeader>
        <CardContent className="p-0">
          {q.isLoading ? (
            <div className="p-4 space-y-2">
              {[...Array(4)].map((_, i) => <Skeleton key={i} className="h-9 w-full" />)}
            </div>
          ) : issues.length === 0 ? (
            <div className="flex items-center justify-center h-24 text-xs text-muted-foreground">
              未修正コード問題なし
            </div>
          ) : (
            <div className="overflow-x-auto">
              <Table>
                <TableHeader>
                  <TableRow className="border-border hover:bg-transparent">
                    <TableHead className="text-xs text-muted-foreground w-[110px]">実行 ID</TableHead>
                    <TableHead className="text-xs text-muted-foreground w-[90px] text-right">イテレーション</TableHead>
                    <TableHead className="text-xs text-muted-foreground w-[90px] text-right">未修正件数</TableHead>
                    <TableHead className="text-xs text-muted-foreground text-right w-[100px]">日時</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {issues.map((issue) => (
                    <IssueRow key={`${issue.execution_id}-${issue.timestamp}`} issue={issue} />
                  ))}
                </TableBody>
              </Table>
            </div>
          )}
        </CardContent>
      </Card>
    </div>
  )
}

/* ── sub-components ─────────────────────────────────────────────────────── */

function DaysSelector({
  value,
  onChange,
}: {
  value: number
  onChange: (v: number) => void
}) {
  return (
    <div className="flex items-center gap-1 rounded-md border border-border bg-secondary p-0.5">
      {DAYS_OPTIONS.map((opt) => (
        <button
          key={opt.value}
          type="button"
          onClick={() => onChange(opt.value)}
          className={`px-3 py-1 rounded text-xs font-medium transition-colors ${
            value === opt.value
              ? "bg-primary text-primary-foreground"
              : "text-muted-foreground hover:text-foreground"
          }`}
        >
          {opt.label}
        </button>
      ))}
    </div>
  )
}

function IssueRow({ issue }: { issue: UnfixedCodeIssue }) {
  return (
    <TableRow className="border-border hover:bg-accent/40">
      <TableCell className="py-3">
        <Link
          to={`/executions/${issue.execution_id}`}
          className="font-mono text-xs text-primary hover:underline"
        >
          {issue.execution_id.slice(0, 12)}…
        </Link>
      </TableCell>
      <TableCell className="py-3 text-right text-xs tabular text-muted-foreground">
        {issue.iteration ?? "—"}
      </TableCell>
      <TableCell className="py-3 text-right text-xs tabular">
        <span className={`font-medium ${issue.code_related_unfixed_count >= 3 ? "text-red-400" : "text-orange-400"}`}>
          {issue.code_related_unfixed_count}
        </span>
      </TableCell>
      <TableCell className="py-3 text-right text-xs tabular text-muted-foreground">
        <span title={fmtDatetime(issue.timestamp)}>{fmtRelative(issue.timestamp)}</span>
      </TableCell>
    </TableRow>
  )
}
