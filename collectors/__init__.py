import os
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
USER_AGENT = "us-stock-recommender/0.1 (personal research tool)"


def replace_retry(tmp, path, tries: int = 8) -> None:
    """임시 파일을 본 파일 자리로 옮긴다. Windows 에서는 다른 프로그램이 잠깐 파일을 열고 있으면 실패해서 몇 번 다시 시도한다."""
    import time

    for i in range(tries):
        try:
            tmp.replace(path)
            return
        except PermissionError:
            time.sleep(0.15 * (i + 1))
    tmp.replace(path)


def load_config(name: str) -> dict:
    with open(ROOT / "config" / f"{name}.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_env() -> None:
    """.env 가 있으면 읽어 환경변수로 올린다 (이미 설정된 값은 덮어쓰지 않는다)."""
    path = ROOT / ".env"
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))
