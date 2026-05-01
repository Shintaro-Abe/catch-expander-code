import { Badge } from "@/components/ui/badge"

const CONFIG: Record<string, { label: string; cls: string }> = {
  success: { label: "成功",   cls: "border-green-500/30 bg-green-500/10 text-green-400" },
  failed:  { label: "失敗",   cls: "border-red-500/30  bg-red-500/10  text-red-400"   },
  running: { label: "実行中", cls: "border-sky-500/30  bg-sky-500/10  text-sky-400"   },
  pending: { label: "待機中", cls: "border-zinc-500/30 bg-zinc-500/10 text-zinc-400"  },
  error:   { label: "エラー", cls: "border-red-500/30  bg-red-500/10  text-red-400"   },
}

export function StatusBadge({ status }: { status: string }) {
  const { label, cls } = CONFIG[status] ?? { label: status, cls: "border-zinc-500/30 bg-zinc-500/10 text-zinc-400" }
  return (
    <Badge variant="outline" className={`text-xs font-medium ${cls}`}>
      {label}
    </Badge>
  )
}
