import { UploadDropzone } from "@/components/upload-dropzone"
import { DocumentRow } from "@/components/document-row"
import { useDocuments } from "@/hooks/use-documents"

export function HomePage() {
  const { documents, loading, error, upsertLocal } = useDocuments()

  return (
    <div className="space-y-6">
      <UploadDropzone onUploaded={upsertLocal} />

      {error && <p className="text-sm text-destructive">{error}</p>}

      <div className="space-y-2">
        {loading && <p className="text-sm text-muted-foreground">Loading…</p>}
        {!loading && documents.length === 0 && (
          <p className="text-sm text-muted-foreground">No documents yet. Upload a PDF above.</p>
        )}
        {documents.map((d) => <DocumentRow key={d.id} doc={d} />)}
      </div>
    </div>
  )
}
