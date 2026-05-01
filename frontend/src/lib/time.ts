import type { Period } from "@/api/types"

export function periodToRange(period: Period): { from: string; to: string } {
  const now = new Date()
  const from = new Date(now)
  if (period === "24h") from.setHours(from.getHours() - 24)
  else if (period === "7d") from.setDate(from.getDate() - 7)
  else from.setDate(from.getDate() - 30)
  return {
    from: from.toISOString().replace(/\.\d{3}Z$/, ".000Z"),
    to:   now.toISOString().replace(/\.\d{3}Z$/, ".000Z"),
  }
}

export function durationMs(createdAt: string, completedAt?: string): number | null {
  if (!completedAt) return null
  return new Date(completedAt).getTime() - new Date(createdAt).getTime()
}

export function fmtDuration(ms: number | null): string {
  if (ms == null) return "—"
  const s = Math.round(ms / 1000)
  if (s >= 3600) return `${Math.floor(s / 3600)}h ${Math.floor((s % 3600) / 60)}m`
  if (s >= 60)   return `${Math.floor(s / 60)}m ${s % 60}s`
  return `${s}s`
}

export function fmtRelative(iso: string | null): string {
  if (!iso) return "—"
  const diff = Date.now() - new Date(iso).getTime()
  if (diff < 0) return "just now"
  const m = Math.floor(diff / 60_000)
  const h = Math.floor(diff / 3_600_000)
  const d = Math.floor(diff / 86_400_000)
  if (d > 0)  return `${d}日前`
  if (h > 0)  return `${h}時間前`
  if (m > 0)  return `${m}分前`
  return "たった今"
}

export function fmtDatetime(iso: string | null): string {
  if (!iso) return "—"
  return new Date(iso).toLocaleString("ja-JP", {
    year: "numeric", month: "2-digit", day: "2-digit",
    hour: "2-digit", minute: "2-digit",
  })
}
