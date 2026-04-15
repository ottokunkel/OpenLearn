import { BrowserRouter, Routes, Route, Navigate } from "react-router-dom"
import { AuthContext, useAuthProvider } from "@/hooks/use-auth"
import { AuthGuard } from "@/components/auth-guard"
import { AppLayout } from "@/components/app-layout"
import { LoginPage } from "@/pages/login"
import { HomePage } from "@/pages/home"

export default function App() {
  const auth = useAuthProvider()
  return (
    <AuthContext.Provider value={auth}>
      <BrowserRouter>
        <Routes>
          <Route path="/login" element={<LoginPage />} />
          <Route element={<AuthGuard />}>
            <Route element={<AppLayout />}>
              <Route path="/" element={<HomePage />} />
              <Route path="*" element={<Navigate to="/" replace />} />
            </Route>
          </Route>
        </Routes>
      </BrowserRouter>
    </AuthContext.Provider>
  )
}
