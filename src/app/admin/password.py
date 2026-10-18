"""Generate a salted password hash with: python -m app.admin.password."""

import base64
import getpass
import hashlib
import secrets

ITERATIONS = 600_000


def hash_password(password: str) -> str:
    if len(password) < 12:
        raise ValueError("Hasło musi mieć co najmniej 12 znaków.")
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), ITERATIONS)
    return f"pbkdf2_sha256${ITERATIONS}${salt}${base64.b64encode(digest).decode()}"


def valid_hash(value: str) -> bool:
    try:
        algorithm, rounds, salt, digest = value.split("$")
        return (
            algorithm == "pbkdf2_sha256"
            and ITERATIONS <= int(rounds) <= 2_000_000
            and len(salt) == 32
            and len(base64.b64decode(digest, validate=True)) == 32
        )
    except (ValueError, TypeError):
        return False


def verify_password(password: str, encoded: str) -> bool:
    if not valid_hash(encoded):
        return False
    _, rounds, salt, digest = encoded.split("$")
    result = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), int(rounds))
    return secrets.compare_digest(result, base64.b64decode(digest))


if __name__ == "__main__":
    first = getpass.getpass("Nowe hasło administratora (min. 12 znaków): ")
    if first != getpass.getpass("Powtórz hasło: "):
        raise SystemExit("Hasła nie są takie same.")
    print(hash_password(first))
