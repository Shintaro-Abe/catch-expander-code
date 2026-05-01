import { createContext, useContext, type ReactNode } from "react"
import { useQuery } from "@tanstack/react-query"

import { endpoints } from "@/api/endpoints"
import type { AuthMeResponse } from "@/api/types"

const AuthContext = createContext<AuthMeResponse | null>(null)

export function AuthProvider({ children }: { children: ReactNode }) {
  const q = useQuery({
    queryKey: ["authMe"],
    queryFn: () => endpoints.authMe(),
    staleTime: 4 * 60_000,
    refetchInterval: 5 * 60_000,
    refetchOnWindowFocus: true,
    retry: false,
  })

  if (q.isPending) {
    return (
      <div className="flex items-center justify-center min-h-screen bg-background">
        <span className="text-sm text-muted-foreground">認証中…</span>
      </div>
    )
  }

  // isError means 401 → client.ts already triggered redirect to /api/v1/auth/login
  if (q.isError || !q.data) return null

  return (
    <AuthContext.Provider value={q.data}>
      {children}
    </AuthContext.Provider>
  )
}

export function useAuth(): AuthMeResponse {
  const ctx = useContext(AuthContext)
  if (!ctx) throw new Error("useAuth must be used inside AuthProvider")
  return ctx
}
