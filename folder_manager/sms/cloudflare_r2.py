from __future__ import annotations

from pathlib import Path
from typing import Dict


class R2ConfigError(RuntimeError):
    pass


def missing_r2_settings(values: Dict[str, str]) -> tuple[str, ...]:
    required = (
        ("account_id", "Cloudflare Account ID"),
        ("access_key_id", "R2 Access Key ID"),
        ("secret_access_key", "R2 Secret Access Key"),
        ("bucket_name", "R2 Bucket Name"),
        ("public_base_url", "Public Base URL"),
    )
    return tuple(label for key, label in required if not str(values.get(key) or "").strip())


def _missing_upload_settings(values: Dict[str, str]) -> tuple[str, ...]:
    required = (
        ("account_id", "Cloudflare Account ID"),
        ("access_key_id", "R2 Access Key ID"),
        ("secret_access_key", "R2 Secret Access Key"),
        ("bucket_name", "R2 Bucket Name"),
    )
    return tuple(label for key, label in required if not str(values.get(key) or "").strip())


def upload_file_to_r2(
    *,
    account_id: str,
    access_key_id: str,
    secret_access_key: str,
    bucket_name: str,
    object_key: str,
    file_path: str,
    content_type: str = "application/octet-stream",
) -> None:
    values = {
        "account_id": account_id,
        "access_key_id": access_key_id,
        "secret_access_key": secret_access_key,
        "bucket_name": bucket_name,
    }
    missing = _missing_upload_settings(values)
    if missing:
        raise R2ConfigError("Missing Cloudflare R2 settings: " + ", ".join(missing))
    source = Path(file_path)
    if not source.is_file():
        raise FileNotFoundError(str(source))
    try:
        import boto3
    except ImportError as exc:
        raise R2ConfigError("boto3 is required for Cloudflare R2 uploads.") from exc

    endpoint_url = f"https://{str(account_id).strip()}.r2.cloudflarestorage.com"
    client = boto3.client(
        "s3",
        endpoint_url=endpoint_url,
        aws_access_key_id=str(access_key_id).strip(),
        aws_secret_access_key=str(secret_access_key).strip(),
        region_name="auto",
    )
    client.upload_file(
        str(source),
        str(bucket_name).strip(),
        str(object_key).lstrip("/"),
        ExtraArgs={"ContentType": content_type},
    )
