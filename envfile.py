"""Loads .env into os.environ.

Lives in its own module so every entrypoint can call it - fetch.py runs the
whole pipeline in-process, but weather.py / trend_summary.py / swim_advisor.py /
bench_models.py are also runnable on their own, and under cron there is no
shell profile to inherit anything from.
"""
import os
from pathlib import Path

HERE = Path(__file__).resolve().parent


def load_dotenv(path: Path | None = None):
    path = path or HERE / ".env"
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def env_flag(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}
