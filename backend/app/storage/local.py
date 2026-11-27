from pathlib import Path


class LocalStorage:
    """Filesystem stand-in for S3, used by tests and keyless local runs."""

    def __init__(self, root: str):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        path = (self.root / key).resolve()
        if not str(path).startswith(str(self.root.resolve())):
            raise ValueError("invalid storage key")
        return path

    def put(self, key: str, data: bytes, content_type: str) -> None:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    def get(self, key: str) -> bytes:
        return self._path(key).read_bytes()

    def presign(self, key: str, expires: int = 3600) -> str | None:
        return None

    def ensure_bucket(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
