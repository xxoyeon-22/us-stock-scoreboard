"""사이트 로그인 계정 관리. 비밀번호는 PBKDF2 해시로만 web/auth.json 에 저장된다.

  python tools/users.py add 아이디 --admin      관리자 계정 (비밀번호는 입력창에서 묻는다)
  python tools/users.py add 아이디              일반 계정
  python tools/users.py remove 아이디
  python tools/users.py list

주의: 정적 사이트(GitHub Pages)라 이 로그인은 ‘가벼운 잠금’이다. auth.json 이 공개되어 있어
해시를 가져가 오프라인으로 맞춰 볼 수 있고, 짧은 비밀번호(예: 숫자 4자리)는 몇 초 안에 풀린다.
진짜 접근 통제가 필요하면 Cloudflare Access 같은 서버 쪽 로그인을 쓴다.
"""
import argparse
import getpass
import hashlib
import json
import re
import secrets
import sys
from pathlib import Path

PATH = Path(__file__).resolve().parent.parent / "web" / "auth.json"
ITERATIONS = 200_000


def load() -> dict:
    try:
        data = json.loads(PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    data.setdefault("v", 1)
    data.setdefault("iterations", ITERATIONS)
    data.setdefault("users", [])
    return data


def save(data: dict) -> None:
    PATH.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")


def hash_password(password: str, salt_hex: str, iterations: int) -> str:
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt_hex), iterations, 32).hex()


def add_user(user_id: str, password: str, admin: bool) -> None:
    user_id = user_id.strip().lower()
    if not re.fullmatch(r"[a-z0-9_.-]{2,32}", user_id):
        raise SystemExit("아이디는 영문 소문자·숫자·_.- 2~32자로 만드세요.")
    if len(password) < 4:
        raise SystemExit("비밀번호가 너무 짧습니다.")
    data = load()
    salt = secrets.token_hex(16)
    entry = {"id": user_id, "role": "admin" if admin else "user", "salt": salt, "hash": hash_password(password, salt, data["iterations"])}
    data["users"] = [u for u in data["users"] if u["id"] != user_id] + [entry]
    save(data)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("add")
    a.add_argument("id")
    a.add_argument("--admin", action="store_true")
    a.add_argument("--password")
    r = sub.add_parser("remove")
    r.add_argument("id")
    sub.add_parser("list")
    args = p.parse_args()
    if args.cmd == "add":
        pw = args.password or getpass.getpass("비밀번호: ")
        add_user(args.id, pw, args.admin)
        print(f"{args.id.lower()} 계정을 {'관리자' if args.admin else '일반'}으로 저장했습니다.")
    elif args.cmd == "remove":
        data = load()
        data["users"] = [u for u in data["users"] if u["id"] != args.id.lower()]
        save(data)
        print("삭제했습니다.")
    else:
        for u in load()["users"]:
            print(u["id"], u["role"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
