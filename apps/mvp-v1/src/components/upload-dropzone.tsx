import { useRef, useState, type DragEvent, type ChangeEvent } from "react"
import { Button } from "@/components/ui/button"
import { useUpload } from "@/hooks/use-upload"
import type { Document } from "@/types/database"

export function UploadDropzone({ onUploaded }: { onUploaded: (d: Document) => void }) {
  const inputRef = useRef<HTMLInputElement>(null)
  const [dragOver, setDragOver] = useState(false)
  const { upload, uploading, error } = useUpload()

  async function handleFiles(files: FileList | null) {
    if (!files || files.length === 0) return
    for (const f of Array.from(files)) {
      const d = await upload(f)
      if (d) onUploaded(d)
    }
  }

  return (
    <div
      onDragOver={(e: DragEvent) => { e.preventDefault(); setDragOver(true) }}
      onDragLeave={() => setDragOver(false)}
      onDrop={(e: DragEvent) => { e.preventDefault(); setDragOver(false); handleFiles(e.dataTransfer.files) }}
      className={`rounded-lg border-2 border-dashed p-8 text-center transition ${
        dragOver ? "border-primary bg-primary/5" : "border-muted-foreground/30"
      }`}
    >
      <p className="text-sm text-muted-foreground mb-3">Drop a PDF here, or</p>
      <Button type="button" onClick={() => inputRef.current?.click()} disabled={uploading}>
        {uploading ? "Uploading…" : "Choose file"}
      </Button>
      <input
        ref={inputRef}
        type="file"
        accept="application/pdf"
        multiple
        className="hidden"
        onChange={(e: ChangeEvent<HTMLInputElement>) => handleFiles(e.target.files)}
      />
      {error && <p className="mt-3 text-sm text-destructive">{error}</p>}
    </div>
  )
}
