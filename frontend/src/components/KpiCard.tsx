import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { Skeleton } from "@/components/ui/skeleton"

interface Props {
  title: string
  value: string | null
  sub?: string
  loading?: boolean
}

export function KpiCard({ title, value, sub, loading }: Props) {
  return (
    <Card className="bg-card border-border">
      <CardHeader className="pb-2 pt-4 px-4">
        <CardTitle className="text-[11px] font-medium text-muted-foreground uppercase tracking-wider">
          {title}
        </CardTitle>
      </CardHeader>
      <CardContent className="px-4 pb-4">
        {loading ? (
          <>
            <Skeleton className="h-8 w-28 mb-1" />
            <Skeleton className="h-3 w-20" />
          </>
        ) : (
          <>
            <div className="text-2xl font-semibold tabular text-foreground">{value ?? "—"}</div>
            {sub && <div className="text-xs text-muted-foreground mt-1">{sub}</div>}
          </>
        )}
      </CardContent>
    </Card>
  )
}
