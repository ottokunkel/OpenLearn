import { useState } from "react"
import { formatDistanceToNow } from "date-fns"
import { Button } from "@/components/ui/button"
import { StatusBadge } from "@/components/status-badge"
import { downloadArtifact } from "@/lib/download"
import { labelFor } from "@/lib/artifact-labels"
import type { Document } from "@/types/database"

function ArtifactButtons({ artifacts }: { artifacts: Record<string, string> }) {
  const [busy, setBusy] = useState<string | null>(null)

  async function handle(key: string, path: string) {
    setBusy(key)
    try {
      const url = await downloadArtifact(path)
      const a = document.createElement("a")
      a.href = url
      a.download = path.split("/").pop() ?? key
      a.rel = "noopener noreferrer"
      document.body.appendChild(a)
      a.click()
      a.remove()
    } finally {
      setBusy(null)
    }
  }

  const keys = Object.keys(artifacts).sort()
  if (keys.length === 0) return null

  return (
    <div className="flex flex-wrap gap-2">
      {keys.map((k) => (
        <Button
          key={k}
          size="sm"
          variant="outline"
          disabled={busy === k}
          onClick={() => handle(k, artifacts[k])}
        >
          {busy === k ? "…" : labelFor(k)}
        </Button>
      ))}
    </div>
  )
}

export function DocumentRow({ doc }: { doc: Document }) {
  return (
    <div className="flex items-start justify-between gap-4 rounded-md border p-4">
      <div className="min-w-0">
        <div className="font-medium truncate">{doc.filename}</div>
        <div className="mt-1 text-xs text-muted-foreground">
          {formatDistanceToNow(new Date(doc.created_at), { addSuffix: true })}
          {doc.page_count != null && <span> · {doc.page_count} pages</span>}
          {doc.pipeline && <span> · {doc.pipeline}</span>}
        </div>
        {doc.status === "failed" && doc.error_message && (
          <p className="mt-2 text-xs text-destructive break-words">{doc.error_message}</p>
        )}
      </div>
      <div className="flex items-center gap-2 shrink-0">
        {doc.status === "completed" && <ArtifactButtons artifacts={doc.artifacts} />}
        <StatusBadge status={doc.status} />
      </div>
    </div>
  )
}
