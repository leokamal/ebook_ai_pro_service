"""Supabase Storage helpers for E-Book AI Pro (REST via httpx, streaming: PDFs never sit in RAM).

They use the service-role key, which BYPASSES storage RLS: callers must have verified ownership of `path`
(the ingestion service pins it to `{user_id}/{document_id}/{file_name}` first).

Environment: SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY.
"""
import os


def _service_key() -> str:
    key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
    if not key or not os.environ.get("SUPABASE_URL"):
        raise RuntimeError("SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY must be set")
    return key


def _object_url(bucket: str, path: str, prefix: str = "object") -> str:
    from urllib.parse import quote
    return f"{os.environ['SUPABASE_URL'].rstrip('/')}/storage/v1/{prefix}/{bucket}/{quote(path, safe='/')}"


def _headers() -> dict:
    key = _service_key()
    return {"Authorization": f"Bearer {key}", "apikey": key}


def inspect_object(bucket: str, path: str):
    """(size_in_bytes, first_bytes) of an object, or None if it does not exist. Reads ~8 bytes."""
    import httpx
    with httpx.stream("GET", _object_url(bucket, path), headers=_headers(), timeout=20) as r:
        if r.status_code in (400, 404):
            return None
        r.raise_for_status()
        size = int(r.headers.get("content-length", "-1"))
        head = b""
        for chunk in r.iter_bytes():
            head += chunk
            if len(head) >= 8:
                break
        return size, head[:8]


def download_to_path(bucket: str, path: str, dest: str, max_bytes: int) -> int:
    """Stream an object to `dest`; aborts if it exceeds max_bytes. Returns the byte count."""
    import httpx
    total = 0
    with httpx.stream("GET", _object_url(bucket, path), headers=_headers(), timeout=60) as r:
        r.raise_for_status()
        with open(dest, "wb") as f:
            for chunk in r.iter_bytes(1024 * 256):
                total += len(chunk)
                if total > max_bytes:
                    raise ValueError("object exceeds max size")
                f.write(chunk)
    return total


def remove_object(bucket: str, path: str) -> None:
    import httpx
    url = f"{os.environ['SUPABASE_URL'].rstrip('/')}/storage/v1/object/{bucket}"
    r = httpx.request("DELETE", url, headers=_headers(), json={"prefixes": [path]}, timeout=20)
    r.raise_for_status()
