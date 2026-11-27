from app.config import get_settings
from app.storage.local import LocalStorage
from app.storage.s3store import S3Storage


def get_store():
    settings = get_settings()
    if settings.storage_backend == "local":
        return LocalStorage(settings.local_storage_dir)
    return S3Storage(
        endpoint=settings.s3_endpoint,
        public_endpoint=settings.s3_public_endpoint,
        bucket=settings.s3_bucket,
        access_key=settings.s3_access_key,
        secret_key=settings.s3_secret_key,
        region=settings.s3_region,
    )
