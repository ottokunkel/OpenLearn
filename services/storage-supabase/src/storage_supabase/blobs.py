from supabase import Client, create_client


class SupabaseBlobStore:
    def __init__(self, url: str, service_role_key: str, bucket: str):
        self._c: Client = create_client(url, service_role_key)
        self._bucket = bucket

    def download(self, path: str) -> bytes:
        return self._c.storage.from_(self._bucket).download(path)

    def upload(
        self,
        path: str,
        content: bytes,
        content_type: str,
        *,
        upsert: bool = True,
    ) -> None:
        self._c.storage.from_(self._bucket).upload(
            path,
            content,
            {
                "content-type": content_type,
                "upsert": "true" if upsert else "false",
            },
        )
