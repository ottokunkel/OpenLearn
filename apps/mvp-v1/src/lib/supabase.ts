import { createClient } from "@supabase/supabase-js"

const supabaseUrl = import.meta.env.VITE_SUPABASE_URL
const anonKey = import.meta.env.VITE_SUPABASE_ANON_KEY

if (!supabaseUrl || !anonKey) {
  throw new Error(
    "Missing VITE_SUPABASE_URL or VITE_SUPABASE_ANON_KEY — add them to the repo-root .env",
  )
}

export const supabase = createClient(supabaseUrl, anonKey, {
  db: { schema: "doc_worker_v2" },
})

export const STORAGE_BUCKET = "documents"
