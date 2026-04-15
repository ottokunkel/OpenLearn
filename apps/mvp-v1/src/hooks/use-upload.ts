import { useCallback, useState } from "react"
import { supabase, STORAGE_BUCKET } from "@/lib/supabase"
import { useAuth } from "@/hooks/use-auth"
import type { Document } from "@/types/database"

const MAX_BYTES = 50 * 1024 * 1024 // 50 MB — matches the v1 edge function limit

interface UploadState {
  uploading: boolean
  error: string | null
}

export function useUpload() {
  const { user } = useAuth()
  const [state, setState] = useState<UploadState>({ uploading: false, error: null })

  const upload = useCallback(
    async (file: File): Promise<Document | null> => {
      if (!user) {
        setState({ uploading: false, error: "Not signed in" })
        return null
      }
      if (!file.name.toLowerCase().endsWith(".pdf")) {
        setState({ uploading: false, error: "Only PDF files are supported" })
        return null
      }
      if (file.size > MAX_BYTES) {
        setState({ uploading: false, error: "File exceeds 50 MB limit" })
        return null
      }

      setState({ uploading: true, error: null })

      const documentId = crypto.randomUUID()
      const filePath = `${user.id}/${documentId}/${file.name}`

      const { error: uploadErr } = await supabase.storage
        .from(STORAGE_BUCKET)
        .upload(filePath, file, { contentType: "application/pdf", upsert: false })

      if (uploadErr) {
        setState({ uploading: false, error: `Upload failed: ${uploadErr.message}` })
        return null
      }

      const { data: inserted, error: insertErr } = await supabase
        .from("documents")
        .insert({
          id: documentId,
          user_id: user.id,
          filename: file.name,
          file_path: filePath,
        })
        .select()
        .single<Document>()

      if (insertErr) {
        // Best-effort cleanup of the orphaned storage object.
        await supabase.storage.from(STORAGE_BUCKET).remove([filePath])
        setState({ uploading: false, error: `Insert failed: ${insertErr.message}` })
        return null
      }

      setState({ uploading: false, error: null })
      return inserted
    },
    [user],
  )

  return { ...state, upload }
}
