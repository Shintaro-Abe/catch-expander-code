export function fmtTokens(n: number | null | undefined): string {
  if (n == null) return "—"
  return n >= 1_000 ? `${(n / 1_000).toFixed(1)}k` : String(n)
}

export function fmtCost(usd: number | null | undefined): string {
  if (usd == null) return "—"
  return `$${usd.toFixed(4)}`
}

export function fmtRate(r: number | null | undefined): string {
  if (r == null) return "—"
  return `${(r * 100).toFixed(1)}%`
}
