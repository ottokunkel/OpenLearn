export type DocumentStatus =
  | "pending"
  | "processing"
  | "retrying"
  | "completed"
  | "failed"

export interface Document {
  id: string
  user_id: string
  filename: string
  file_path: string
  status: DocumentStatus
  error_message: string | null
  page_count: number | null
  pipeline: string | null
  artifacts: Record<string, string>
  metadata: Record<string, unknown>
  created_at: string
  updated_at: string
}
