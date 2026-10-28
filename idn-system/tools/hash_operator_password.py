"""Generate a PBKDF2-SHA256 value for IDN_OPERATOR_PASSWORD_HASH.

Run this only in a trusted operator terminal.  It writes no password or hash
to repository files.
"""
from __future__ import annotations

import base64
import getpass
import hashlib
import secrets

password = getpass.getpass("Operator password: ")
confirm = getpass.getpass("Confirm password: ")
if not password or password != confirm:
    raise SystemExit("Passwords did not match")
iterations = 600_000
salt = secrets.token_bytes(16)
digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
encode = lambda value: base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")
print(f"pbkdf2_sha256${iterations}${encode(salt)}${encode(digest)}")
