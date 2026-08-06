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
        if not line or line.startswith("#"):
            continue
        # `export KEY=value` is a common way to write .env files (it's valid
        # shell too) - strip the keyword before splitting, or KEY would come
        # out as "export KEY" and the real name never gets set.
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        if "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), _clean_value(v))


def _clean_value(raw: str) -> str:
    """Strips surrounding quotes, and a trailing ` # comment` on unquoted values.

    People write inline comments in .env files whether or not the format invites
    it; without this, `OLLAMA_HOST=http://host:11434  # the Mac` silently sets a
    URL with the comment glued on. Quoted values are left alone so a password
    containing ' #' survives.
    """
    v = raw.strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
        return v[1:-1]
    return v.split(" #", 1)[0].split("\t#", 1)[0].strip()


def env_flag(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}
