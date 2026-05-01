import { useState, useMemo } from "react"
import { useQuery } from "@tanstack/react-query"
import { Link } from "react-router-dom"
import { Search, ChevronLeft, ChevronRight } from "lucide-react"

import { endpoints } from "@/api/endpoints"
import type { Execution, Period } from "@/api/types"
import { PeriodSelector } from "@/components/PeriodSelector"
import { StatusBadge } from "@/components/StatusBadge"
import { Card, CardContent } from "@/components/ui/card"
import { Skeleton } from "@/components/ui/skeleton"
import {
  Table, TableBody, TableCell, TableHead, TableHeader, TableRow,
} from "@/components/ui/table"
import { periodToRange, durationMs, fmtDuration, fmtRelative, fmtDatetime } from "@/lib/time"

/* ── constants ─────────────────────────────────────────────────────────── */

const PAGE_SIZE = 20

const STATUS_TABS = [
  { value: "",        label: "すべて"   },
  { value: "success", label: "成功"     },
  { value: "failed",  label: "失敗"     },
  { value: "running", label: "実行中"   },
]

/* ── component ─────────────────────────────────────────────────────────── */

export function ExecutionList() {
  const [period, setPeriod]       = useState<Period>("7d")
  const [statusFilter, setStatus] = useState("")
  const [search, setSearch]       = useState("")
  const [page, setPage]           = useState(1)

  const range = useMemo(() => periodToRange(period), [period])

  const q = useQuery({
    queryKey: ["executions", period, statusFilter],
    queryFn: () => endpoints.executions({
      limit: 200,
      from: range.from,
      to: range.to,
      status: statusFilter || undefined,
    }),
    staleTime: 30_000,
  })

  /* client-side topic search */
  const filtered = useMemo(() => {
    const rows = q.data?.data ?? []
    if (!search.trim()) return rows
    const kw = search.toLowerCase()
    return rows.filter((e) => e.topic.toLowerCase().includes(kw))
  }, [q.data, search])

  const totalPages = Math.max(1, Math.ceil(filtered.length / PAGE_SIZE))
  const safePage   = Math.min(page, totalPages)
  const pageRows   = filtered.slice((safePage - 1) * PAGE_SIZE, safePage * PAGE_SIZE)

  function handlePeriodChange(p: Period) {
    setPeriod(p)
    setPage(1)
  }
  function handleStatusChange(s: string) {
    setStatus(s)
    setPage(1)
  }
  function handleSearch(v: string) {
    setSearch(v)
    setPage(1)
  }

  return (
    <div className="space-y-4">
      {/* Page header */}
      <div className="flex items-center justify-between">
        <h1 className="text-lg font-semibold text-foreground">実行一覧</h1>
        <PeriodSelector value={period} onChange={handlePeriodChange} />
      </div>

      {/* Filters */}
      <div className="flex flex-col sm:flex-row gap-3">
        {/* Status tabs */}
        <div className="flex items-center gap-1 rounded-md border border-border bg-secondary p-0.5">
          {STATUS_TABS.map((t) => (
            <button
              key={t.value}
              type="button"
              onClick={() => handleStatusChange(t.value)}
              className={`px-3 py-1 rounded text-xs font-medium transition-colors ${
                statusFilter === t.value
                  ? "bg-primary text-primary-foreground"
                  : "text-muted-foreground hover:text-foreground"
              }`}
            >
              {t.label}
            </button>
          ))}
        </div>

        {/* Topic search */}
        <div className="relative flex-1 max-w-xs">
          <Search size={13} className="absolute left-2.5 top-1/2 -translate-y-1/2 text-muted-foreground" />
          <input
            type="text"
            placeholder="トピックを検索..."
            value={search}
            onChange={(e) => handleSearch(e.target.value)}
            className="w-full pl-7 pr-3 py-1.5 text-xs rounded-md border border-border bg-input text-foreground placeholder:text-muted-foreground focus:outline-none focus:ring-1 focus:ring-ring"
          />
        </div>
      </div>

      {/* Table */}
      <Card className="bg-card border-border">
        <CardContent className="p-0">
          {q.isLoading ? (
            <LoadingSkeleton />
          ) : filtered.length === 0 ? (
            <EmptyState />
          ) : (
            <>
              <Table>
                <TableHeader>
                  <TableRow className="border-border hover:bg-transparent">
                    <TableHead className="text-xs text-muted-foreground w-[80px]">状態</TableHead>
                    <TableHead className="text-xs text-muted-foreground w-[110px]">ID</TableHead>
                    <TableHead className="text-xs text-muted-foreground">トピック</TableHead>
                    <TableHead className="text-xs text-muted-foreground text-right w-[80px]">実行時間</TableHead>
                    <TableHead className="text-xs text-muted-foreground text-right w-[100px]">開始日時</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {pageRows.map((ex) => (
                    <ExecutionRow key={ex.execution_id} ex={ex} />
                  ))}
                </TableBody>
              </Table>

              {/* Pagination */}
              {totalPages > 1 && (
                <Pagination
                  page={safePage}
                  total={totalPages}
                  count={filtered.length}
                  onPrev={() => setPage((p) => Math.max(1, p - 1))}
                  onNext={() => setPage((p) => Math.min(totalPages, p + 1))}
                />
              )}
            </>
          )}
        </CardContent>
      </Card>
    </div>
  )
}

/* ── sub-components ─────────────────────────────────────────────────────── */

function ExecutionRow({ ex }: { ex: Execution }) {
  const dur = durationMs(ex.created_at, ex.completed_at)
  return (
    <TableRow className="border-border hover:bg-accent/40 cursor-pointer">
      <TableCell className="py-3">
        <StatusBadge status={ex.status} />
      </TableCell>
      <TableCell className="py-3">
        <Link
          to={`/executions/${ex.execution_id}`}
          className="font-mono text-xs text-primary hover:underline"
        >
          {ex.execution_id.slice(0, 12)}…
        </Link>
      </TableCell>
      <TableCell className="py-3 max-w-0">
        <span
          className="block text-xs text-foreground truncate max-w-[420px]"
          title={ex.topic}
        >
          {ex.topic}
        </span>
      </TableCell>
      <TableCell className="py-3 text-right text-xs tabular text-muted-foreground">
        {fmtDuration(dur)}
      </TableCell>
      <TableCell className="py-3 text-right text-xs tabular text-muted-foreground">
        <span title={fmtDatetime(ex.created_at)}>{fmtRelative(ex.created_at)}</span>
      </TableCell>
    </TableRow>
  )
}

function Pagination({
  page, total, count, onPrev, onNext,
}: { page: number; total: number; count: number; onPrev: () => void; onNext: () => void }) {
  return (
    <div className="flex items-center justify-between px-4 py-3 border-t border-border">
      <span className="text-xs text-muted-foreground">{count} 件</span>
      <div className="flex items-center gap-2">
        <button
          type="button"
          onClick={onPrev}
          disabled={page === 1}
          className="p-1 rounded text-muted-foreground hover:text-foreground disabled:opacity-30 disabled:cursor-not-allowed transition-colors"
        >
          <ChevronLeft size={14} />
        </button>
        <span className="text-xs tabular text-muted-foreground">
          {page} / {total}
        </span>
        <button
          type="button"
          onClick={onNext}
          disabled={page === total}
          className="p-1 rounded text-muted-foreground hover:text-foreground disabled:opacity-30 disabled:cursor-not-allowed transition-colors"
        >
          <ChevronRight size={14} />
        </button>
      </div>
    </div>
  )
}

function LoadingSkeleton() {
  return (
    <div className="p-4 space-y-2">
      {[...Array(8)].map((_, i) => <Skeleton key={i} className="h-10 w-full" />)}
    </div>
  )
}

function EmptyState() {
  return (
    <div className="flex items-center justify-center h-32 text-xs text-muted-foreground">
      この期間の実行履歴なし
    </div>
  )
}
