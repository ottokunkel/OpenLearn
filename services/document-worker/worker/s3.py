"""S3-compatible storage client. Works with AWS S3, MinIO, DO Spaces, etc."""

import json
import logging
import os
import tempfile

import boto3

logger = logging.getLogger(__name__)


class S3Client:
    def __init__(
        self,
        endpoint_url: str | None,
        region: str,
        access_key: str,
        secret_key: str,
        download_dir: str,
    ):
        self._client = boto3.client(
            "s3",
            endpoint_url=endpoint_url or None,
            region_name=region,
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
        )
        self._download_dir = download_dir
        os.makedirs(download_dir, exist_ok=True)

    def download_file(self, bucket: str, key: str, job_id: str) -> str:
        """Download file from S3 to local path. Returns local file path."""
        filename = f"{job_id}_{os.path.basename(key)}"
        local_path = os.path.join(self._download_dir, filename)
        self._client.download_file(bucket, key, local_path)
        logger.info("Downloaded s3://%s/%s -> %s", bucket, key, local_path)
        return local_path

    def upload_json_streaming(self, bucket: str, key: str, data: dict) -> None:
        """Serialize dict to JSON via temp file, then upload.

        Uses json.dump() which writes incrementally — never holds the full
        JSON string in memory alongside the dict.
        """
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False, dir=self._download_dir
        ) as tmp:
            tmp_path = tmp.name
            json.dump(data, tmp, ensure_ascii=False)

        try:
            self._client.upload_file(
                tmp_path,
                bucket,
                key,
                ExtraArgs={"ContentType": "application/json"},
            )
            logger.info("Uploaded JSON to s3://%s/%s", bucket, key)
        finally:
            os.unlink(tmp_path)

    def upload_text(self, bucket: str, key: str, text: str) -> None:
        """Upload a text string (e.g., markdown) to S3."""
        self._client.put_object(
            Bucket=bucket,
            Key=key,
            Body=text.encode("utf-8"),
            ContentType="text/markdown",
        )
        logger.info("Uploaded text to s3://%s/%s", bucket, key)
