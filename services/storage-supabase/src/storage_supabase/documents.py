from typing import Any

from supabase import Client, create_client


class SupabaseDocumentStore:
    def __init__(self, url: str, service_role_key: str):
        self._c: Client = create_client(url, service_role_key)

    def _table(self):
        return self._c.schema("doc_worker_v2").table("documents")

    def _update(self, document_id: str, fields: dict[str, Any]) -> None:
        self._table().update(fields).eq("id", document_id).execute()

    def mark_processing(self, document_id: str) -> None:
        self._update(document_id, {"status": "processing", "error_message": None})

    def mark_retrying(
        self,
        document_id: str,
        attempt: int,
        max_attempts: int,
        error: str,
    ) -> None:
        self._update(
            document_id,
            {
                "status": "retrying",
                "error_message": f"attempt {attempt}/{max_attempts}: {error[:1000]}",
            },
        )

    def mark_failed(self, document_id: str, error: str) -> None:
        self._update(
            document_id,
            {"status": "failed", "error_message": error[:1000]},
        )

    def mark_completed(
        self,
        document_id: str,
        *,
        page_count: int,
        artifacts: dict[str, str],
        pipeline: str,
    ) -> None:
        self._update(
            document_id,
            {
                "status": "completed",
                "page_count": page_count,
                "artifacts": artifacts,
                "pipeline": pipeline,
                "error_message": None,
            },
        )
