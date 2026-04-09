import os

from supabase import Client

from worker.config import Settings


class StorageClient:
    def __init__(self, supabase_client: Client, settings: Settings):
        self.client = supabase_client
        self.source_bucket = settings.source_bucket
        self.output_bucket = settings.output_bucket
        self.download_dir = settings.download_dir
        os.makedirs(self.download_dir, exist_ok=True)

    def download_pdf(self, job_id: str, storage_path: str) -> str:
        data = self.client.storage.from_(self.source_bucket).download(storage_path)
        filename = f"{job_id}_{os.path.basename(storage_path)}"
        local_path = os.path.join(self.download_dir, filename)
        with open(local_path, "wb") as f:
            f.write(data)
        return local_path

    def upload_json(self, storage_path: str, data: bytes) -> None:
        self.client.storage.from_(self.output_bucket).upload(
            storage_path,
            data,
            {"content-type": "application/json", "upsert": "true"},
        )
