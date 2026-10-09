"""Storage keys contain generated IDs only; uploaded filenames are never paths."""

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

import boto3

if TYPE_CHECKING:
    from mypy_boto3_s3.client import S3Client

from app.core.settings import Settings


class ObjectStorage(Protocol):
    async def put(self, key: str, body: bytes, content_type: str) -> None: ...
    async def get(self, key: str) -> bytes: ...
    async def delete(self, key: str) -> None: ...


class LocalObjectStorage:
    def __init__(self, root: str) -> None:
        self.root = Path(root).resolve()

    def _path(self, key: str) -> Path:
        if not key or any(c not in "0123456789abcdef/-" for c in key) or ".." in key:
            raise ValueError("Invalid storage key")
        result = (self.root / key).resolve()
        if not result.is_relative_to(self.root):
            raise ValueError("Invalid storage key")
        return result

    async def put(self, key: str, body: bytes, content_type: str) -> None:
        path = self._path(key)

        def write() -> None:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(body)

        await asyncio.to_thread(write)

    async def get(self, key: str) -> bytes:
        return await asyncio.to_thread(self._path(key).read_bytes)

    async def delete(self, key: str) -> None:
        await asyncio.to_thread(self._path(key).unlink, missing_ok=True)


class S3ObjectStorage:
    def __init__(self, settings: Settings) -> None:
        self.bucket = settings.knowledge_s3_bucket
        self.client: S3Client = boto3.client(  # pyright: ignore[reportUnknownMemberType]
            "s3",
            region_name=settings.knowledge_s3_region or None,
            endpoint_url=settings.knowledge_s3_endpoint or None,
        )

    async def put(self, key: str, body: bytes, content_type: str) -> None:
        await asyncio.to_thread(
            self.client.put_object,
            Bucket=self.bucket,
            Key=key,
            Body=body,
            ContentType=content_type,
            ServerSideEncryption="AES256",
        )

    async def get(self, key: str) -> bytes:
        def read() -> bytes:
            response = self.client.get_object(Bucket=self.bucket, Key=key)
            try:
                return response["Body"].read()
            finally:
                response["Body"].close()

        return await asyncio.to_thread(read)

    async def delete(self, key: str) -> None:
        await asyncio.to_thread(self.client.delete_object, Bucket=self.bucket, Key=key)


def build_storage(settings: Settings) -> ObjectStorage:
    if settings.knowledge_storage_backend == "s3":
        return S3ObjectStorage(settings)
    return LocalObjectStorage(settings.knowledge_local_path)
