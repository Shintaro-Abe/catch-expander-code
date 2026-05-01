import { Outlet } from "react-router-dom"

export function Layout() {
  return (
    <div className="min-h-screen bg-background text-foreground">
      <main className="container mx-auto px-4 py-6">
        <Outlet />
      </main>
    </div>
  )
}
