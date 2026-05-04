import { useState } from "react"
import { useParams, Link } from "react-router-dom"
import { useQuery } from "@tanstack/react-query"
import {
  ChevronLeft, MessageSquare, GitBranch, CheckCircle2,
  Flag, Zap, AlertTriangle, ThumbsUp, Circle, ChevronDown,
  ChevronUp, Clock, User, Hash, Cpu,
} from "lucide-react"
import type { LucideIcon } from "lucide-react"

import { endpoints } from "@/api/endpoints"
import type { DashboardEvent, Deliverable } from "@/api/types"
import { StatusBadge } from "@/components/StatusBadge"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { Skeleton } from "@/components/ui/skeleton"
import { durationMs, fmtDuration, fmtRelative, fmtDatetime } from "@/lib/time"

/* ── event type config ──────────────────────────────────────────────────── */

interface EventConfig { icon: LucideIcon; label: string; color: string }

const EVENT_CONFIG: Record<string, EventConfig> = {
  topic_received:      { icon: MessageSquare, label: "トピック受信",      color: "text-blue-400"    },
  workflow_planned:    { icon: GitBranch,     label: "ワークフロー計画",   color: "text-indigo-400"  },
  review_completed:    { icon: CheckCircle2,  label: "レビュー完了",       color: "text-green-400"   },
  execution_completed: { icon: Flag,          label: "実行完了",           color: "text-emerald-400" },
  api_call_completed:  { icon: Zap,           label: "API 呼び出し",       color: "text-yellow-400"  },
  rate_limit_hit:      { icon: AlertTriangle, label: "レート制限",         color: "text-orange-400"  },
  feedback_received:   { icon: ThumbsUp,      label: "フィードバック",     color: "text-pink-400"    },
}

function getEventConfig(type: string): EventConfig {
  return EVENT_CONFIG[type] ?? { icon: Circle, label: type, color: "text-muted-foreground" }
}

/* ── token helpers ──────────────────────────────────────────────────────── */

function fmtTokens(n: unknown): string {
  if (n == null || typeof n !== "number") return "—"
  return n >= 1_000 ? `${(n / 1_000).toFixed(1)}k` : String(n)
}

function fmtCost(usd: unknown): string {
  if (usd == null || typeof usd !== "number") return "—"
  return `$${usd.toFixed(4)}`
}

function TokenChip({ label, value, highlight = false }: { label: string; value: string; highlight?: boolean }) {
  return (
    <span className={`inline-flex items-center gap-1 px-1.5 py-0.5 rounded text-[10px] ${
      highlight ? "bg-indigo-900/40 text-indigo-300" : "bg-secondary text-muted-foreground"
    }`}>
      <span className="text-muted-foreground">{label}</span>
      <span className="tabular font-mono">{value}</span>
    </span>
  )
}

function EventTokenSummary({ payload, eventType }: { payload: Record<string, unknown>; eventType: string }) {
  if (eventType === "api_call_completed") {
    const input = payload.input_tokens
    const output = payload.output_tokens
    const total = payload.total_tokens
    if (total == null && input == null) return null
    return (
      <div className="flex items-center gap-1 flex-wrap mt-0.5">
        <Cpu size={10} className="text-muted-foreground shrink-0" />
        {input != null && <TokenChip label="in" value={fmtTokens(input)} />}
        {output != null && <TokenChip label="out" value={fmtTokens(output)} />}
        {total != null && <TokenChip label="total" value={fmtTokens(total)} highlight />}
      </div>
    )
  }
  if (eventType === "execution_completed") {
    const tokens = payload.total_tokens_used
    const cost = payload.total_cost_usd
    if (tokens == null && cost == null) return null
    return (
      <div className="flex items-center gap-1 flex-wrap mt-0.5">
        <Cpu size={10} className="text-muted-foreground shrink-0" />
        {tokens != null && <TokenChip label="total" value={fmtTokens(tokens)} highlight />}
        {cost != null && <TokenChip label="cost" value={fmtCost(cost)} />}
      </div>
    )
  }
  return null
}

/* ── component ─────────────────────────────────────────────────────────── */

export function ExecutionDetail() {
  const { executionId } = useParams<{ executionId: string }>()
  const [expandedEvents, setExpandedEvents] = useState<Set<string>>(new Set())

  const execQ = useQuery({
    queryKey: ["execution", executionId],
    queryFn: () => endpoints.execution(executionId!),
    enabled: !!executionId,
    staleTime: 30_000,
  })

  const eventsQ = useQuery({
    queryKey: ["executionEvents", executionId],
    queryFn: () => endpoints.executionEvents(executionId!),
    enabled: !!executionId,
    staleTime: 30_000,
  })

  const execution    = execQ.data?.data.execution
  const deliverables = execQ.data?.data.deliverables ?? []
  const events       = eventsQ.data?.data ?? []

  function toggleEvent(sk: string) {
    setExpandedEvents((prev) => {
      const next = new Set(prev)
      next.has(sk) ? next.delete(sk) : next.add(sk)
      return next
    })
  }

  const dur = execution ? durationMs(execution.created_at, execution.completed_at) : null

  return (
    <div className="space-y-4">
      {/* Back */}
      <Link
        to="/executions"
        className="inline-flex items-center gap-1 text-xs text-muted-foreground hover:text-foreground transition-colors"
      >
        <ChevronLeft size={13} />
        実行一覧に戻る
      </Link>

      {/* Header card */}
      {execQ.isLoading ? (
        <Card className="bg-card border-border">
          <CardContent className="p-4 space-y-2">
            <Skeleton className="h-5 w-48" />
            <Skeleton className="h-4 w-full" />
            <Skeleton className="h-4 w-64" />
          </CardContent>
        </Card>
      ) : execution ? (
        <Card className="bg-card border-border">
          <CardContent className="p-4 space-y-2">
            <div className="flex items-center gap-3 flex-wrap">
              <StatusBadge status={execution.status} />
              <span className="font-mono text-xs text-muted-foreground">{execution.execution_id}</span>
            </div>
            <p className="text-sm font-medium text-foreground leading-snug">{execution.topic}</p>
            <div className="flex items-center gap-4 flex-wrap text-xs text-muted-foreground">
              <span className="flex items-center gap-1">
                <Clock size={11} />
                {fmtDatetime(execution.created_at)}
                {execution.completed_at && (
                  <> → {fmtDatetime(execution.completed_at)}</>
                )}
              </span>
              <span>{fmtDuration(dur)}</span>
            </div>
          </CardContent>
        </Card>
      ) : null}

      <div className="grid grid-cols-1 lg:grid-cols-3 gap-4">
        {/* Event timeline */}
        <div className="lg:col-span-2 space-y-4">
          <Card className="bg-card border-border">
            <CardHeader className="px-4 pt-4 pb-2">
              <CardTitle className="text-sm font-semibold text-foreground">イベントタイムライン</CardTitle>
            </CardHeader>
            <CardContent className="px-4 pb-4">
              {eventsQ.isLoading ? (
                <div className="space-y-3">
                  {[...Array(4)].map((_, i) => <Skeleton key={i} className="h-10 w-full" />)}
                </div>
              ) : events.length === 0 ? (
                <p className="text-xs text-muted-foreground">イベントなし</p>
              ) : (
                <Timeline events={events} expanded={expandedEvents} onToggle={toggleEvent} />
              )}
            </CardContent>
          </Card>
        </div>

        {/* Right sidebar */}
        <div className="space-y-4">
          {/* Execution meta */}
          <Card className="bg-card border-border">
            <CardHeader className="px-4 pt-4 pb-2">
              <CardTitle className="text-sm font-semibold text-foreground">実行情報</CardTitle>
            </CardHeader>
            <CardContent className="px-4 pb-4">
              {execQ.isLoading ? (
                <div className="space-y-2">
                  {[...Array(4)].map((_, i) => <Skeleton key={i} className="h-4 w-full" />)}
                </div>
              ) : execution ? (
                <dl className="space-y-2.5 text-xs">
                  <MetaRow icon={User}  label="ユーザー ID"      value={execution.user_id} />
                  <MetaRow icon={Hash}  label="カテゴリ"          value={execution.category} />
                  <MetaRow icon={MessageSquare} label="Slack チャンネル" value={execution.slack_channel} />
                  {execution.intent && (
                    <div>
                      <dt className="text-muted-foreground mb-0.5">インテント</dt>
                      <dd className="text-foreground">{execution.intent}</dd>
                    </div>
                  )}
                  {execution.perspectives && execution.perspectives.length > 0 && (
                    <div>
                      <dt className="text-muted-foreground mb-0.5">視点</dt>
                      <dd className="flex flex-wrap gap-1">
                        {execution.perspectives.map((p) => (
                          <span key={p} className="px-1.5 py-0.5 rounded bg-secondary text-muted-foreground text-[10px]">{p}</span>
                        ))}
                      </dd>
                    </div>
                  )}
                  {execution.deliverable_types && execution.deliverable_types.length > 0 && (
                    <div>
                      <dt className="text-muted-foreground mb-0.5">成果物タイプ</dt>
                      <dd className="flex flex-wrap gap-1">
                        {execution.deliverable_types.map((d) => (
                          <span key={d} className="px-1.5 py-0.5 rounded bg-secondary text-muted-foreground text-[10px]">{d}</span>
                        ))}
                      </dd>
                    </div>
                  )}
                </dl>
              ) : null}
            </CardContent>
          </Card>

          {/* Workflow plan */}
          {execution?.workflow_plan && (
            <JsonCard title="ワークフロープラン" value={execution.workflow_plan} />
          )}

          {/* Deliverables */}
          {deliverables.length > 0 && (
            <Card className="bg-card border-border">
              <CardHeader className="px-4 pt-4 pb-2">
                <CardTitle className="text-sm font-semibold text-foreground">
                  成果物 <span className="text-muted-foreground font-normal">({deliverables.length})</span>
                </CardTitle>
              </CardHeader>
              <CardContent className="px-4 pb-4">
                <ul className="space-y-2">
                  {deliverables.map((d, i) => (
                    <DeliverableRow key={d.deliverable_id ?? i} d={d} />
                  ))}
                </ul>
              </CardContent>
            </Card>
          )}
        </div>
      </div>
    </div>
  )
}

/* ── sub-components ─────────────────────────────────────────────────────── */

function Timeline({
  events,
  expanded,
  onToggle,
}: {
  events: DashboardEvent[]
  expanded: Set<string>
  onToggle: (sk: string) => void
}) {
  return (
    <div className="relative">
      {/* vertical line */}
      <div className="absolute left-3 top-0 bottom-0 w-px bg-border" aria-hidden />
      <ul className="space-y-4">
        {events.map((ev) => {
          const { icon: Icon, label, color } = getEventConfig(ev.event_type)
          const isExpanded = expanded.has(ev.sk)
          const hasPayload = Object.keys(ev.payload ?? {}).length > 0
          return (
            <li key={ev.sk} className="relative pl-8">
              {/* dot */}
              <div className="absolute left-0 flex items-center justify-center w-6 h-6 rounded-full bg-card border border-border">
                <Icon size={11} className={color} />
              </div>

              <div className="space-y-1">
                <div className="flex items-center gap-2 flex-wrap">
                  <span className="text-xs font-medium text-foreground">{label}</span>
                  <span
                    className="text-[10px] text-muted-foreground"
                    title={fmtDatetime(ev.timestamp)}
                  >
                    {fmtRelative(ev.timestamp)}
                  </span>
                  <span className="text-[10px] px-1.5 py-0.5 rounded bg-secondary text-muted-foreground">
                    {ev.status_at_emit}
                  </span>
                </div>

                <EventTokenSummary payload={ev.payload ?? {}} eventType={ev.event_type} />

                {hasPayload && (
                  <button
                    type="button"
                    onClick={() => onToggle(ev.sk)}
                    className="flex items-center gap-1 text-[10px] text-muted-foreground hover:text-foreground transition-colors"
                  >
                    {isExpanded ? <ChevronUp size={10} /> : <ChevronDown size={10} />}
                    {isExpanded ? "詳細を隠す" : "詳細を表示"}
                  </button>
                )}

                {isExpanded && hasPayload && (
                  <pre className="text-[11px] font-mono bg-muted/30 rounded p-2 overflow-x-auto whitespace-pre-wrap break-words text-muted-foreground mt-1">
                    {JSON.stringify(ev.payload, null, 2)}
                  </pre>
                )}
              </div>
            </li>
          )
        })}
      </ul>
    </div>
  )
}

function MetaRow({
  icon: Icon,
  label,
  value,
}: {
  icon: LucideIcon
  label: string
  value: string | undefined
}) {
  if (!value) return null
  return (
    <div className="flex items-start gap-2">
      <Icon size={11} className="mt-0.5 shrink-0 text-muted-foreground" />
      <div className="min-w-0">
        <span className="text-muted-foreground">{label}: </span>
        <span className="text-foreground break-all">{value}</span>
      </div>
    </div>
  )
}

function JsonCard({ title, value }: { title: string; value: Record<string, unknown> }) {
  const [open, setOpen] = useState(false)
  return (
    <Card className="bg-card border-border">
      <CardHeader className="px-4 pt-4 pb-2">
        <button
          type="button"
          className="flex items-center justify-between w-full text-left"
          onClick={() => setOpen((v) => !v)}
          aria-label={open ? "閉じる" : "開く"}
        >
          <CardTitle className="text-sm font-semibold text-foreground">{title}</CardTitle>
          {open ? <ChevronUp size={13} className="text-muted-foreground" /> : <ChevronDown size={13} className="text-muted-foreground" />}
        </button>
      </CardHeader>
      {open && (
        <CardContent className="px-4 pb-4">
          <pre className="text-[11px] font-mono bg-muted/30 rounded p-2 overflow-x-auto whitespace-pre-wrap break-words text-muted-foreground">
            {JSON.stringify(value, null, 2)}
          </pre>
        </CardContent>
      )}
    </Card>
  )
}

function DeliverableRow({ d }: { d: Deliverable }) {
  return (
    <li className="text-xs">
      <div className="flex items-center gap-2">
        {d.deliverable_type && (
          <span className="px-1.5 py-0.5 rounded bg-secondary text-muted-foreground text-[10px]">
            {d.deliverable_type}
          </span>
        )}
        <span className="text-foreground truncate">{d.title ?? d.deliverable_id ?? "—"}</span>
      </div>
      {d.created_at && (
        <span className="text-[10px] text-muted-foreground">{fmtRelative(d.created_at as string)}</span>
      )}
    </li>
  )
}
