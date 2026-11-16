import base64
import json
import secrets
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tools import users


def make_code(uid="kim", password="correct-horse", iterations=200_000, **over):
    salt = secrets.token_hex(16)
    req = {"v": 1, "id": uid, "salt": salt, "hash": users.hash_password(password, salt, iterations), "iterations": iterations, **over}
    return base64.b64encode(json.dumps(req).encode()).decode()


class ApproveTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        patcher = mock.patch.object(users, "PATH", Path(self.tmp.name) / "auth.json")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.tmp.cleanup)

    def test_approve_adds_plain_user_never_admin(self):
        self.assertEqual(users.approve(make_code("kim", role="admin")), "kim")
        (u,) = users.load()["users"]
        self.assertEqual((u["id"], u["role"]), ("kim", "user"))
        self.assertNotIn("correct-horse", json.dumps(users.load()))

    def test_stored_hash_matches_password(self):
        code = make_code("lee", "my-password-1")
        users.approve(code)
        u = users.load()["users"][0]
        self.assertEqual(users.hash_password("my-password-1", u["salt"], 200_000), u["hash"])
        self.assertNotEqual(users.hash_password("wrong-password", u["salt"], 200_000), u["hash"])

    def test_rejects_duplicates_bad_ids_and_tampering(self):
        users.approve(make_code("kim"))
        for bad in (make_code("kim"), make_code("Bad ID!"), make_code("x1", iterations=1000), make_code("x2", hash="zz"), "not-base64!!"):
            with self.assertRaises(SystemExit):
                users.approve(bad)
        self.assertEqual(len(users.load()["users"]), 1)


if __name__ == "__main__":
    unittest.main()
