import type { Period } from "@/api/types"

const OPTIONS: { value: Period; label: string }[] = [
  { value: "24h", label: "24h" },
  { value: "7d",  label: "7d"  },
  { value: "30d", label: "30d" },
]

interface Props {
  value: Period
  onChange: (p: Period) => void
}

export function PeriodSelector({ value, onChange }: Props) {
  return (
    <div className="flex items-center gap-1 rounded-md border border-border bg-secondary p-0.5">
      {OPTIONS.map((o) => (
        <button
          key={o.value}
          type="button"
          onClick={() => onChange(o.value)}
          className={`px-3 py-1 rounded text-xs font-medium transition-colors ${
            value === o.value
              ? "bg-primary text-primary-foreground"
              : "text-muted-foreground hover:text-foreground"
          }`}
        >
          {o.label}
        </button>
      ))}
    </div>
  )
}
