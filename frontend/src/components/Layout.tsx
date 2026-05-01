import { NavLink, Outlet } from "react-router-dom"
import { LayoutDashboard, List, Star, AlertTriangle, LogOut, User } from "lucide-react"
import { useAuth } from "@/contexts/AuthContext"

const NAV = [
  { to: "/dashboard",      icon: LayoutDashboard, label: "ダッシュボード" },
  { to: "/executions",     icon: List,             label: "実行一覧"       },
  { to: "/review-quality", icon: Star,             label: "レビュー品質"   },
  { to: "/errors",         icon: AlertTriangle,    label: "エラー"         },
]

export function Layout() {
  const { user_name } = useAuth()
  return (
    <div className="flex min-h-screen bg-background">
      {/* Sidebar */}
      <aside className="hidden md:flex w-[220px] shrink-0 flex-col border-r border-sidebar-border bg-sidebar">
        <div className="px-4 py-4 border-b border-sidebar-border">
          <span className="text-sm font-semibold text-sidebar-foreground leading-tight">
            Catch-Expander
            <span className="block text-[11px] font-normal text-muted-foreground">監視ダッシュボード</span>
          </span>
        </div>
        <nav className="flex-1 p-2 space-y-0.5">
          {NAV.map(({ to, icon: Icon, label }) => (
            <NavLink
              key={to}
              to={to}
              className={({ isActive }) =>
                `flex items-center gap-2.5 px-3 py-2 rounded-md text-sm transition-colors ${
                  isActive
                    ? "bg-sidebar-accent text-primary font-medium"
                    : "text-muted-foreground hover:bg-sidebar-accent hover:text-sidebar-foreground"
                }`
              }
            >
              <Icon size={15} className="shrink-0" />
              {label}
            </NavLink>
          ))}
        </nav>
        <div className="p-4 border-t border-sidebar-border space-y-2">
          <div className="flex items-center gap-2 text-xs text-muted-foreground">
            <User size={12} className="shrink-0" />
            <span className="truncate">{user_name}</span>
          </div>
          <a
            href="/api/v1/auth/logout"
            className="flex items-center gap-2 text-xs text-muted-foreground hover:text-foreground transition-colors"
          >
            <LogOut size={12} className="shrink-0" />
            ログアウト
          </a>
        </div>
      </aside>

      {/* Main content */}
      <main className="flex-1 min-w-0 overflow-auto">
        <div className="max-w-[1280px] mx-auto px-6 py-6">
          <Outlet />
        </div>
      </main>
    </div>
  )
}
