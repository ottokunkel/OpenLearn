from datetime import datetime, timezone

from supabase import create_client, Client

from worker.config import Settings


class Database:
    def __init__(self, settings: Settings):
        self.client: Client = create_client(settings.supabase_url, settings.supabase_service_key)

    def claim_job(self, worker_id: str) -> dict | None:
        response = self.client.rpc("claim_next_conversion_job", {"p_worker_id": worker_id}).execute()
        if response.data:
            return response.data[0]
        return None

    def complete_job(self, job_id: str) -> None:
        now = datetime.now(timezone.utc).isoformat()
        self.client.table("conversion_jobs").update({
            "status": "completed",
            "completed_at": now,
            "updated_at": now,
        }).eq("id", job_id).execute()

    def fail_job(self, job_id: str, error_message: str, retryable: bool = True) -> None:
        now = datetime.now(timezone.utc).isoformat()

        if retryable:
            resp = self.client.table("conversion_jobs").select("attempts, max_attempts").eq("id", job_id).execute()
            job = resp.data[0] if resp.data else None
            if job and job["attempts"] < job["max_attempts"]:
                self.client.table("conversion_jobs").update({
                    "status": "pending",
                    "error_message": error_message,
                    "updated_at": now,
                    "worker_id": None,
                }).eq("id", job_id).execute()
                return

        self.client.table("conversion_jobs").update({
            "status": "failed",
            "error_message": error_message,
            "completed_at": now,
            "updated_at": now,
        }).eq("id", job_id).execute()

    def insert_document(self, job_id: str, doc_name: str, page_count: int,
                        storage_path: str, markdown_preview: str) -> str:
        response = self.client.table("converted_documents").insert({
            "job_id": job_id,
            "doc_name": doc_name,
            "page_count": page_count,
            "storage_path": storage_path,
            "markdown_preview": markdown_preview,
        }).execute()
        return response.data[0]["id"]
