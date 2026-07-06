import { useQuery } from "@tanstack/react-query"
import { Info } from "lucide-react"

import { endpoints } from "@/api/endpoints"
import type { LearnedPreference, MyProfile } from "@/api/types"
import { fmtRelative } from "@/lib/time"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { Skeleton } from "@/components/ui/skeleton"

// 5W1H 6 軸キーとラベルの対応 (backend get_my_profile.app._PROFILE_KEYS と整合)
const AXIS_FIELDS: ReadonlyArray<{ key: keyof Omit<MyProfile, "user_id" | "learned_preferences" | "updated_at">; label: string }> = [
  { key: "role",                label: "役割・職業" },
  { key: "interests",           label: "関心分野" },
  { key: "expertise",           label: "専門・得意領域" },
  { key: "learning_goals",      label: "学習の目的" },
  { key: "background",          label: "背景・状況" },
  { key: "output_preferences",  label: "受け取り方の好み" },
]

// 成果物区分 6 値の表示名 (backend feedback/scope.py:SCOPE_DELIVERABLE_LABELS_JA と整合)
const SCOPE_DELIVERABLE_LABELS: Record<string, string> = {
  code: "コード",
  research_report: "調査レポート",
  architecture_design: "アーキテクチャ設計書",
  comparison_table: "比較表",
  cost_estimate: "料金見積もり",
  procedure_guide: "手順書",
}

function ScopeBadges({ scope }: { scope: LearnedPreference["scope"] }) {
  const labels = [
    ...scope.categories,
    ...scope.deliverables.map((d) => SCOPE_DELIVERABLE_LABELS[d] ?? d),
  ]
  if (labels.length === 0) {
    return (
      <span className="inline-block px-1.5 py-0.5 mr-2 rounded text-xs bg-muted text-muted-foreground align-middle">
        汎用
      </span>
    )
  }
  return (
    <>
      {labels.map((label) => (
        <span
          key={label}
          className="inline-block px-1.5 py-0.5 mr-1 last:mr-2 rounded text-xs bg-primary/10 text-primary align-middle"
        >
          {label}
        </span>
      ))}
    </>
  )
}

function AxisRow({ label, value }: { label: string; value: string | null }) {
  return (
    <div className="grid grid-cols-1 md:grid-cols-[200px_1fr] gap-2 py-3 border-b border-border last:border-b-0">
      <div className="text-sm font-medium text-foreground">{label}</div>
      {value ? (
        <div className="text-sm text-foreground whitespace-pre-wrap break-words">{value}</div>
      ) : (
        <div className="text-sm italic text-muted-foreground">未設定</div>
      )}
    </div>
  )
}

export function MyProfile() {
  const q = useQuery({
    queryKey: ["my-profile"],
    queryFn: () => endpoints.myProfile().then((r) => r.data),
    staleTime: 60_000,
  })

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-semibold">マイプロファイル</h1>
        <p className="text-sm text-muted-foreground mt-1">
          AI に伝わっているあなたの 5W1H 6 軸プロファイル (read-only) と、自動学習された好みの一覧です。
        </p>
      </div>

      {/* 編集導線 banner */}
      <div className="flex items-start gap-3 p-4 rounded-md border border-border bg-muted/30">
        <Info size={16} className="shrink-0 mt-0.5 text-muted-foreground" />
        <div className="text-sm">
          編集は Slack で <code className="px-1.5 py-0.5 rounded bg-muted text-foreground font-mono text-xs">@CatchExpander profile</code> を実行してください。
        </div>
      </div>

      {q.isLoading && (
        <Card>
          <CardContent className="pt-6 space-y-3">
            {AXIS_FIELDS.map((f) => (
              <Skeleton key={f.key} className="h-16 w-full" />
            ))}
          </CardContent>
        </Card>
      )}

      {q.isError && (
        <Card>
          <CardContent className="pt-6">
            <div className="text-sm text-destructive">
              プロファイルの取得に失敗しました。時間を置いて再読み込みしてください。
            </div>
          </CardContent>
        </Card>
      )}

      {q.data && (
        <>
          <Card>
            <CardHeader>
              <CardTitle>プロファイル</CardTitle>
            </CardHeader>
            <CardContent>
              {AXIS_FIELDS.map((f) => (
                <AxisRow key={f.key} label={f.label} value={q.data[f.key]} />
              ))}
            </CardContent>
          </Card>

          <Card>
            <CardHeader>
              <CardTitle>学習履歴 (learned_preferences)</CardTitle>
            </CardHeader>
            <CardContent>
              {q.data.learned_preferences.length === 0 ? (
                <div className="text-sm italic text-muted-foreground">
                  学習履歴はまだありません。フィードバックを送ると AI が自動で好みを学習します。
                </div>
              ) : (
                <ul className="space-y-2 text-sm">
                  {q.data.learned_preferences.map((pref, i) => (
                    <li key={i} className="text-foreground break-words">
                      <ScopeBadges scope={pref.scope} />
                      {pref.text}
                    </li>
                  ))}
                </ul>
              )}
            </CardContent>
          </Card>

          {q.data.updated_at && (
            <div className="text-xs text-muted-foreground text-right">
              最終更新: {fmtRelative(q.data.updated_at)}
            </div>
          )}
        </>
      )}
    </div>
  )
}
