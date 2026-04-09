"""Tests for worker/s3.py using moto."""

import json

import boto3
import pytest
from moto import mock_aws

from worker.s3 import S3Client

BUCKET = "test-bucket"


@pytest.fixture
def s3(tmp_path):
    with mock_aws():
        boto3.client("s3", region_name="us-east-1").create_bucket(Bucket=BUCKET)
        yield S3Client(
            endpoint_url=None,
            region="us-east-1",
            access_key="testing",
            secret_key="testing",
            download_dir=str(tmp_path / "dl"),
        )


class TestS3Client:
    def test_download_file(self, s3):
        boto3.client("s3", region_name="us-east-1").put_object(
            Bucket=BUCKET, Key="docs/test.pdf", Body=b"PDF"
        )
        path = s3.download_file(BUCKET, "docs/test.pdf", "job-1")
        assert open(path, "rb").read() == b"PDF"
        assert path.endswith("job-1_test.pdf")

    def test_upload_json_streaming(self, s3):
        data = {"key": "value"}
        s3.upload_json_streaming(BUCKET, "out/doc.json", data)
        raw = boto3.client("s3", region_name="us-east-1")
        body = json.loads(raw.get_object(Bucket=BUCKET, Key="out/doc.json")["Body"].read())
        assert body == data

    def test_upload_text(self, s3):
        s3.upload_text(BUCKET, "out/doc.md", "# Hello")
        raw = boto3.client("s3", region_name="us-east-1")
        resp = raw.get_object(Bucket=BUCKET, Key="out/doc.md")
        assert resp["Body"].read().decode() == "# Hello"
