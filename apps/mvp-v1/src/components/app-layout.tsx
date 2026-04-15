import { Outlet } from "react-router-dom"
import { Button } from "@/components/ui/button"
import { useAuth } from "@/hooks/use-auth"

export function AppLayout() {
  const { user, signOut } = useAuth()
  return (
    <div className="min-h-screen flex flex-col">
      <header className="flex items-center justify-between border-b px-6 py-3">
        <div className="font-semibold">OpenLearn</div>
        <div className="flex items-center gap-3 text-sm text-muted-foreground">
          <span>{user?.email}</span>
          <Button variant="outline" size="sm" onClick={signOut}>Sign out</Button>
        </div>
      </header>
      <main className="flex-1 mx-auto w-full max-w-4xl p-6">
        <Outlet />
      </main>
    </div>
  )
}
