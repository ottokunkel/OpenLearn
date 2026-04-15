import { Badge } from "@/components/ui/badge"
import type { DocumentStatus } from "@/types/database"

const VARIANTS: Record<DocumentStatus, { label: string; className: string }> = {
  pending:    { label: "Pending",    className: "bg-muted text-muted-foreground" },
  processing: { label: "Processing", className: "bg-blue-500/15 text-blue-700" },
  retrying:   { label: "Retrying",   className: "bg-yellow-500/15 text-yellow-700" },
  completed:  { label: "Completed",  className: "bg-green-500/15 text-green-700" },
  failed:     { label: "Failed",     className: "bg-destructive/15 text-destructive" },
}

export function StatusBadge({ status }: { status: DocumentStatus }) {
  const v = VARIANTS[status]
  return <Badge variant="outline" className={v.className}>{v.label}</Badge>
}
