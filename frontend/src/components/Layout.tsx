import { useState } from "react"
import { NavLink, Outlet } from "react-router-dom"
import { LayoutDashboard, List, Star, AlertTriangle, LogOut, User, Menu } from "lucide-react"
import { useAuth } from "@/contexts/AuthContext"

const NAV = [
  { to: "/dashboard",      icon: LayoutDashboard, label: "ダッシュボード" },
  { to: "/executions",     icon: List,             label: "実行一覧"       },
  { to: "/review-quality", icon: Star,             label: "レビュー品質"   },
  { to: "/errors",         icon: AlertTriangle,    label: "エラー"         },
]

export function Layout() {
  const { user_name } = useAuth()
  const [mobileOpen, setMobileOpen] = useState(false)

  return (
    <div className="flex min-h-screen bg-background">
      <a
        href="#main-content"
        className="sr-only focus:not-sr-only focus:absolute focus:top-2 focus:left-2 focus:z-50 focus:px-3 focus:py-1.5 focus:rounded focus:bg-primary focus:text-primary-foreground focus:text-xs"
      >
        メインコンテンツへスキップ
      </a>

      {/* Mobile header (below md) */}
      <div className="md:hidden fixed top-0 left-0 right-0 z-40 flex flex-col bg-sidebar border-b border-sidebar-border">
        <div className="flex items-center justify-between px-4 py-3">
          <span className="text-sm font-semibold text-sidebar-foreground">Catch-Expander</span>
          <button
            type="button"
            onClick={() => setMobileOpen((v) => !v)}
            className="text-muted-foreground hover:text-sidebar-foreground transition-colors"
            aria-label="メニューを開く"
          >
            <Menu size={18} />
          </button>
        </div>
        {mobileOpen && (
          <nav className="bg-sidebar border-b border-sidebar-border p-2 space-y-0.5">
            {NAV.map(({ to, icon: Icon, label }) => (
              <NavLink
                key={to}
                to={to}
                onClick={() => setMobileOpen(false)}
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
        )}
      </div>

      {/* Sidebar (md and above) */}
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
      <main id="main-content" className="flex-1 min-w-0 overflow-auto pt-[52px] md:pt-0">
        <div className="max-w-[1280px] mx-auto px-6 py-6">
          <Outlet />
        </div>
      </main>
    </div>
  )
}
