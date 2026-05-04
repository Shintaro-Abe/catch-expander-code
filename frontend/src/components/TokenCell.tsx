import type { Execution } from "@/api/types"
import { fmtTokens } from "@/lib/format"

export function TokenCell({ ex }: { ex: Execution }) {
  const total = ex.total_tokens_used ?? null
  const input = ex.total_input_tokens ?? null
  const output = ex.total_output_tokens ?? null
  if (total == null && input == null) return <span className="text-muted-foreground">—</span>
  return (
    <div className="text-right leading-tight">
      <div className="tabular">{fmtTokens(total)}</div>
      {(input != null || output != null) && (
        <div className="text-[10px] text-muted-foreground/60 tabular">
          in {fmtTokens(input)} / out {fmtTokens(output)}
        </div>
      )}
    </div>
  )
}
