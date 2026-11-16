"""사이트 로그인 계정 관리. 비밀번호는 PBKDF2 해시로만 web/auth.json 에 저장된다.

  python tools/users.py add 아이디 --admin      관리자 계정 (비밀번호는 입력창에서 묻는다)
  python tools/users.py add 아이디              일반 계정
  python tools/users.py approve 신청코드         로그인 화면 ‘가입 신청’으로 받은 코드를 승인 (일반 계정)
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


def approve(code: str) -> str:
    """로그인 화면의 ‘신청 코드’를 승인해 일반 계정으로 추가한다 (관리자 권한은 줄 수 없다)."""
    import base64
    try:
        req = json.loads(base64.b64decode(code.strip(), validate=True))
    except Exception:
        raise SystemExit("신청 코드를 읽을 수 없습니다. 처음부터 끝까지 그대로 붙여 넣었는지 확인하세요.")
    data = load()
    uid = str(req.get("id", "")).strip().lower()
    salt, digest = str(req.get("salt", "")), str(req.get("hash", ""))
    if not re.fullmatch(r"[a-z0-9_.-]{2,32}", uid):
        raise SystemExit("신청 코드의 아이디 형식이 올바르지 않습니다.")
    if not re.fullmatch(r"[0-9a-f]{32}", salt) or not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise SystemExit("신청 코드의 해시 형식이 올바르지 않습니다.")
    if req.get("iterations") != data["iterations"]:
        raise SystemExit("신청 코드의 반복 횟수가 이 사이트 설정과 다릅니다. 다시 신청하게 하세요.")
    if any(u["id"] == uid for u in data["users"]):
        raise SystemExit(f"이미 있는 아이디입니다: {uid}")
    data["users"].append({"id": uid, "role": "user", "salt": salt, "hash": digest})
    save(data)
    return uid


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("add")
    a.add_argument("id")
    a.add_argument("--admin", action="store_true")
    a.add_argument("--password")
    ap = sub.add_parser("approve")
    ap.add_argument("code")
    r = sub.add_parser("remove")
    r.add_argument("id")
    sub.add_parser("list")
    args = p.parse_args()
    if args.cmd == "add":
        pw = args.password or getpass.getpass("비밀번호: ")
        add_user(args.id, pw, args.admin)
        print(f"{args.id.lower()} 계정을 {'관리자' if args.admin else '일반'}으로 저장했습니다.")
    elif args.cmd == "approve":
        print(f"{approve(args.code)} 계정을 일반 계정으로 승인했습니다. 커밋·푸시하면 로그인할 수 있습니다.")
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
