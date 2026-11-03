"""【学习标注：对象存储适配器】LocalStorage 与 S3Storage 实现相同接口，业务代码不依赖具体存储。"""
import hashlib
import io
import re
import shutil
from pathlib import Path
from uuid import uuid4

import boto3
from botocore.config import Config


class LocalStorage:
    """Development storage implementing the same bounded streaming contract as S3."""

    def __init__(self, settings):
        self.root = settings.storage_root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def path(self, key):
        if not re.fullmatch(r"[a-zA-Z0-9/_-]+", key):
            raise ValueError("Invalid internal object key")
        path = (self.root / key).resolve()
        if not path.is_relative_to(self.root):
            raise ValueError("Invalid storage path")
        return path

    def initiate(self, key):
        upload_id = uuid4().hex
        self.path("parts/" + upload_id).mkdir(parents=True)
        return upload_id

    def put_part(self, key, upload_id, number, body):
        path = self.path(f"parts/{upload_id}/{number}")
        path.write_bytes(body)
        return hashlib.sha256(body).hexdigest()

    def complete(self, key, upload_id, parts):
        path = self.path(key)
        if path.is_file():
            return  # Completion may have committed to storage before a DB/network failure.
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".pending")
        with temporary.open("wb") as out:
            for part in parts:
                with self.path(f"parts/{upload_id}/{part['PartNumber']}").open("rb") as source:
                    shutil.copyfileobj(source, out, 1024 * 1024)
        temporary.replace(path)
        self.abort(key, upload_id)

    def abort(self, key, upload_id):
        path = self.path("parts/" + upload_id)
        if path.is_dir():
            for part in path.iterdir():
                part.unlink()
            path.rmdir()

    def chunks(self, key, start=0, end=None):
        with self.path(key).open("rb") as stream:
            stream.seek(start)
            remaining = None if end is None else end - start + 1
            while remaining is None or remaining > 0:
                block = stream.read(min(1024 * 1024, remaining) if remaining is not None else 1024 * 1024)
                if not block:
                    break
                if remaining is not None:
                    remaining -= len(block)
                yield block

    def put_file(self, key, fileobj, progress=None):
        path = self.path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("wb") as out:
            while block := fileobj.read(1024 * 1024):
                if progress:
                    progress()
                out.write(block)

    def put_bytes(self, key, data):
        self.put_file(key, io.BytesIO(data))

    def delete(self, key):
        self.path(key).unlink(missing_ok=True)

    def close(self):
        pass  # 本地文件句柄由各方法的 with 语句关闭。


class S3Storage:
    def __init__(self, settings):
        self.bucket = settings.s3_bucket
        self.client = boto3.client("s3", endpoint_url=settings.s3_endpoint,
                                    aws_access_key_id=settings.s3_access_key,
                                    aws_secret_access_key=settings.s3_secret_key,
                                    region_name=settings.s3_region,
                                    config=Config(signature_version="s3v4", s3={"addressing_style": "path"},
                                                  connect_timeout=10, read_timeout=60,
                                                  max_pool_connections=settings.s3_max_pool_connections,
                                                  retries={"max_attempts": 3}))

    def initiate(self, key):
        return self.client.create_multipart_upload(Bucket=self.bucket, Key=key,
                                                   ContentType="application/octet-stream")["UploadId"]

    def put_part(self, key, upload_id, number, body):
        return self.client.upload_part(Bucket=self.bucket, Key=key, UploadId=upload_id,
                                       PartNumber=number, Body=body)["ETag"]

    def complete(self, key, upload_id, parts):
        from botocore.exceptions import ClientError
        try:
            self.client.complete_multipart_upload(Bucket=self.bucket, Key=key, UploadId=upload_id,
                                                  MultipartUpload={"Parts": parts})
        except ClientError as exc:
            if exc.response["Error"]["Code"] != "NoSuchUpload":
                raise
            # Keys are server generated and never reused. A successful HEAD makes retry safe;
            # the subsequent quarantine scan still verifies expected size and SHA-256.
            self.client.head_object(Bucket=self.bucket, Key=key)

    def abort(self, key, upload_id):
        from botocore.exceptions import ClientError
        try:
            self.client.abort_multipart_upload(Bucket=self.bucket, Key=key, UploadId=upload_id)
        except ClientError as exc:
            if exc.response["Error"]["Code"] != "NoSuchUpload":
                raise

    def chunks(self, key, start=0, end=None):
        kwargs = {"Bucket": self.bucket, "Key": key}
        if start or end is not None:
            kwargs["Range"] = f"bytes={start}-{end if end is not None else ''}"
        stream = self.client.get_object(**kwargs)["Body"]
        try:
            yield from stream.iter_chunks(chunk_size=1024 * 1024)
        finally:
            stream.close()

    def put_file(self, key, fileobj, progress=None):
        options = {"Callback": lambda _: progress()} if progress else {}
        self.client.upload_fileobj(fileobj, self.bucket, key, **options)

    def put_bytes(self, key, data):
        self.client.put_object(Bucket=self.bucket, Key=key, Body=data)

    def delete(self, key):
        self.client.delete_object(Bucket=self.bucket, Key=key)

    def close(self):
        self.client.close()


def make_storage(settings):
    return S3Storage(settings) if settings.storage_backend == "s3" else LocalStorage(settings)
