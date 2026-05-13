import os
from pathlib import Path

_ENV_LOADED = False


def load_env_file(filename: str = ".env", *, override: bool = False) -> None:
    """
    Populate os.environ with values from a simple KEY=VALUE .env file.
    Lines starting with # are ignored. Values wrapped in single or double
    quotes have the outer quotes stripped.
    """
    global _ENV_LOADED

    if _ENV_LOADED and not override:
        return

    module_dir = Path(__file__).resolve().parent
    explicit_path = os.getenv("DAMY_PHOTODECK_ENV_PATH", "").strip()
    shared_config_dir = os.getenv("DAMY_SHARED_CONFIG_DIR", "").strip()
    share_root = os.getenv("DAMY_SHARE_ROOT", "").strip()
    candidates = []
    if explicit_path:
        candidates.append(Path(explicit_path))
    if shared_config_dir:
        candidates.extend([
            Path(shared_config_dir) / "photodeck.env",
            Path(shared_config_dir) / filename,
        ])
    if share_root:
        candidates.extend([
            Path(share_root) / "config" / "photodeck.env",
            Path(share_root) / "config" / filename,
        ])
    candidates.extend([
        module_dir / filename,
        module_dir.parent / filename,
    ])

    env_path = next((path for path in candidates if path.exists()), None)

    if env_path is None:
        _ENV_LOADED = True
        return

    with env_path.resolve().open("r", encoding="utf-8") as handle:
        for raw_line in handle:
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue

            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip()

            if not key:
                continue

            if value and len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
                value = value[1:-1]

            if override:
                os.environ[key] = value
            else:
                os.environ.setdefault(key, value)

    _ENV_LOADED = True
