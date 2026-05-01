import { useState, useMemo } from "react"
import { useQuery } from "@tanstack/react-query"
import { Link } from "react-router-dom"

import { endpoints } from "@/api/endpoints"
import type { ErrorItem } from "@/api/types"
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

/* error_type → hue for badge */
const TYPE_COLORS: Record<string, string> = {
  timeout:           "bg-orange-500/15 text-orange-400",
  api_error:         "bg-red-500/15 text-red-400",
  validation_error:  "bg-yellow-500/15 text-yellow-400",
  rate_limit:        "bg-purple-500/15 text-purple-400",
  unknown:           "bg-secondary text-muted-foreground",
}

function typeColor(t: string): string {
  return TYPE_COLORS[t] ?? "bg-secondary text-muted-foreground"
}

/* ── component ─────────────────────────────────────────────────────────── */

export function ErrorList() {
  const [days, setDays]           = useState(7)
  const [typeFilter, setTypeFilter] = useState("")

  const q = useQuery({
    queryKey: ["errors", days],
    queryFn: () => endpoints.errors(days),
    staleTime: 30_000,
  })

  const byType = q.data?.meta.by_type ?? {}
  const allTypes = Object.entries(byType).sort((a, b) => b[1] - a[1])

  const filtered = useMemo(() => {
    const rows = q.data?.data ?? []
    if (!typeFilter) return rows
    return rows.filter((e) => e.error_type === typeFilter)
  }, [q.data, typeFilter])

  function handleDaysChange(v: number) {
    setDays(v)
    setTypeFilter("")
  }

  return (
    <div className="space-y-4">
      {/* Header */}
      <div className="flex items-center justify-between">
        <h1 className="text-lg font-semibold text-foreground">エラー</h1>
        <DaysSelector value={days} onChange={handleDaysChange} />
      </div>

      {/* Error type breakdown */}
      {!q.isLoading && allTypes.length > 0 && (
        <div className="flex flex-wrap gap-2">
          <TypeChip
            label="すべて"
            count={q.data?.meta.total ?? 0}
            active={typeFilter === ""}
            onClick={() => setTypeFilter("")}
            colorCls="bg-secondary text-muted-foreground"
          />
          {allTypes.map(([type, count]) => (
            <TypeChip
              key={type}
              label={type}
              count={count}
              active={typeFilter === type}
              onClick={() => setTypeFilter(typeFilter === type ? "" : type)}
              colorCls={typeColor(type)}
            />
          ))}
        </div>
      )}

      {/* Table */}
      <Card className="bg-card border-border">
        <CardHeader className="px-4 pt-4 pb-2">
          <CardTitle className="text-sm font-semibold text-foreground">
            エラーログ
            {!q.isLoading && (
              <span className="ml-1.5 text-muted-foreground font-normal text-xs">
                ({filtered.length} 件)
              </span>
            )}
          </CardTitle>
        </CardHeader>
        <CardContent className="p-0">
          {q.isLoading ? (
            <div className="p-4 space-y-2">
              {[...Array(5)].map((_, i) => <Skeleton key={i} className="h-9 w-full" />)}
            </div>
          ) : filtered.length === 0 ? (
            <div className="flex items-center justify-center h-24 text-xs text-muted-foreground">
              この期間のエラーなし
            </div>
          ) : (
            <Table>
              <TableHeader>
                <TableRow className="border-border hover:bg-transparent">
                  <TableHead className="text-xs text-muted-foreground w-[100px]">日時</TableHead>
                  <TableHead className="text-xs text-muted-foreground w-[110px]">実行 ID</TableHead>
                  <TableHead className="text-xs text-muted-foreground w-[130px]">エラータイプ</TableHead>
                  <TableHead className="text-xs text-muted-foreground w-[90px]">ステージ</TableHead>
                  <TableHead className="text-xs text-muted-foreground">メッセージ</TableHead>
                  <TableHead className="text-xs text-muted-foreground w-[70px] text-right">回復可否</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {filtered.map((err) => (
                  <ErrorRow key={`${err.execution_id}-${err.timestamp}`} err={err} />
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

function TypeChip({
  label, count, active, onClick, colorCls,
}: {
  label: string
  count: number
  active: boolean
  onClick: () => void
  colorCls: string
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={`inline-flex items-center gap-1.5 px-2.5 py-1 rounded-full text-xs font-medium transition-colors border ${
        active
          ? "border-primary bg-primary/15 text-primary"
          : `border-transparent ${colorCls} hover:border-border`
      }`}
    >
      {label}
      <span className="text-[10px] opacity-70">{count}</span>
    </button>
  )
}

function RecoverableBadge({ val }: { val: boolean | null }) {
  if (val === true)  return <span className="text-[10px] text-green-400">回復可</span>
  if (val === false) return <span className="text-[10px] text-red-400">非回復</span>
  return <span className="text-[10px] text-muted-foreground">—</span>
}

function ErrorRow({ err }: { err: ErrorItem }) {
  return (
    <TableRow className="border-border hover:bg-accent/40">
      <TableCell className="py-3 text-xs tabular text-muted-foreground">
        <span title={fmtDatetime(err.timestamp)}>{fmtRelative(err.timestamp)}</span>
      </TableCell>
      <TableCell className="py-3">
        <Link
          to={`/executions/${err.execution_id}`}
          className="font-mono text-xs text-primary hover:underline"
        >
          {err.execution_id.slice(0, 12)}…
        </Link>
      </TableCell>
      <TableCell className="py-3">
        <span className={`inline-block px-2 py-0.5 rounded text-[10px] font-medium ${typeColor(err.error_type)}`}>
          {err.error_type}
        </span>
      </TableCell>
      <TableCell className="py-3 text-xs text-muted-foreground">
        {err.stage || "—"}
      </TableCell>
      <TableCell className="py-3 max-w-0">
        <span
          className="block text-xs text-foreground truncate max-w-[360px]"
          title={err.error_message}
        >
          {err.error_message || "—"}
        </span>
      </TableCell>
      <TableCell className="py-3 text-right">
        <RecoverableBadge val={err.is_recoverable} />
      </TableCell>
    </TableRow>
  )
}
