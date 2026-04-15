import { useEffect, useState } from "react"
import { supabase } from "@/lib/supabase"
import { useAuth } from "@/hooks/use-auth"
import type { Document } from "@/types/database"

export function useDocuments() {
  const { user } = useAuth()
  const [documents, setDocuments] = useState<Document[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    if (!user) return
    let active = true

    async function load() {
      setLoading(true)
      const { data, error: err } = await supabase
        .from("documents")
        .select("*")
        .order("created_at", { ascending: false })
      if (!active) return
      if (err) setError(err.message)
      else setDocuments((data ?? []) as Document[])
      setLoading(false)
    }
    load()

    const channel = supabase
      .channel(`docs-${user.id}`)
      .on(
        "postgres_changes",
        {
          event: "*",
          schema: "doc_worker_v2",
          table: "documents",
          filter: `user_id=eq.${user.id}`,
        },
        (payload) => {
          setDocuments((prev) => {
            if (payload.eventType === "INSERT") {
              const next = payload.new as Document
              if (prev.some((d) => d.id === next.id)) return prev
              return [next, ...prev]
            }
            if (payload.eventType === "UPDATE") {
              const next = payload.new as Document
              return prev.map((d) => (d.id === next.id ? next : d))
            }
            if (payload.eventType === "DELETE") {
              const id = (payload.old as Document).id
              return prev.filter((d) => d.id !== id)
            }
            return prev
          })
        },
      )
      .subscribe()

    return () => {
      active = false
      supabase.removeChannel(channel)
    }
  }, [user])

  // Convenience to merge in optimistic inserts from the upload hook.
  function upsertLocal(d: Document) {
    setDocuments((prev) => (prev.some((x) => x.id === d.id) ? prev : [d, ...prev]))
  }

  return { documents, loading, error, upsertLocal }
}
