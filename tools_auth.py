"""Local single-owner dashboard authentication."""
import hashlib
import hmac
import json
import os
import re
import secrets
import threading
import time

DATA_DIR = os.getenv("AGATHA_DATA_DIR", os.path.join(os.path.dirname(os.path.abspath(__file__)), "agatha_data"))
ACCOUNT_FILE = os.path.join(DATA_DIR, "account.json")
_salt_bytes = 16
_scrypt_n = 16384
_sessions = set()
_lock = threading.RLock()
SESSION_TTL_SECONDS = 30 * 24 * 60 * 60


class AuthenticationError(ValueError):
    pass


def _load_account():
    try:
        with open(ACCOUNT_FILE, "r", encoding="utf-8") as account_file:
            data = json.load(account_file)
        return data if isinstance(data, dict) and data.get("password_hash") else None
    except (OSError, ValueError):
        return None


def _password_hash(password: str, salt: bytes) -> bytes:
    return hashlib.scrypt(password.encode("utf-8"), salt=salt, n=_scrypt_n, r=8, p=1, dklen=32)


def account_exists() -> bool:
    with _lock:
        return _load_account() is not None


def account_name() -> str:
    with _lock:
        account = _load_account()
        return account.get("name", "") if account else ""


def account_identifier() -> str:
    with _lock:
        account = _load_account()
        return account.get("identifier", account.get("name", "")) if account else ""


def _public_account(account: dict) -> dict:
    return {
        "name": account.get("name", ""),
        "identifier": account.get("identifier", account.get("name", "")),
    }


def _validate_profile(name: str, identifier: str) -> tuple[str, str]:
    name = " ".join((name or "").split())
    identifier = (identifier or "").strip()
    if not name or len(name) > 80:
        raise AuthenticationError("Enter a name between 1 and 80 characters.")
    if not identifier or len(identifier) > 254 or any(char.isspace() for char in identifier):
        raise AuthenticationError("Enter a valid username or email address.")
    if not re.fullmatch(r"[A-Za-z0-9._@+-]+", identifier):
        raise AuthenticationError("Use letters, numbers, dots, underscores, plus signs, or hyphens in the username/email.")
    if "@" in identifier and not re.fullmatch(r"[^@\s]+@[^@\s.]+(?:\.[^@\s.]+)+", identifier):
        raise AuthenticationError("Enter a valid username or email address.")
    return name, identifier


def _save_account(account: dict):
    os.makedirs(DATA_DIR, exist_ok=True)
    temporary_file = ACCOUNT_FILE + ".tmp"
    with open(temporary_file, "w", encoding="utf-8") as account_file:
        json.dump(account, account_file)
    os.chmod(temporary_file, 0o600)
    os.replace(temporary_file, ACCOUNT_FILE)


def _ensure_session_secret(account: dict) -> str:
    secret = account.get("session_secret")
    if not secret:
        secret = secrets.token_hex(32)
        account["session_secret"] = secret
        _save_account(account)
    return secret


def signup(name: str, identifier: str, password: str) -> tuple[str, dict]:
    name, identifier = _validate_profile(name, identifier)
    if len(password or "") < 10 or len(password) > 1024:
        raise AuthenticationError("Use a password between 10 and 1024 characters.")
    with _lock:
        if _load_account():
            raise AuthenticationError("An Agatha owner account already exists. Please sign in.")
        salt = secrets.token_bytes(_salt_bytes)
        account = {
            "name": name,
            "identifier": identifier,
            "salt": salt.hex(),
            "password_hash": _password_hash(password, salt).hex(),
            "session_secret": secrets.token_hex(32),
        }
        _save_account(account)
        token = _new_session(account)
        return token, _public_account(account)


def login(identifier: str, password: str) -> tuple[str, dict]:
    identifier = (identifier or "").strip()
    if len(password or "") > 1024:
        raise AuthenticationError("That password did not match.")
    with _lock:
        account = _load_account()
        if account is None:
            raise AuthenticationError("No owner account exists yet. Create one to get started.")
        try:
            salt = bytes.fromhex(account["salt"])
            expected = bytes.fromhex(account["password_hash"])
        except (KeyError, ValueError):
            raise AuthenticationError("The local account file is invalid. Remove it and create a new account.")
        stored_identifier = account.get("identifier", account.get("name", ""))
        identifier_matches = hmac.compare_digest(identifier.casefold(), stored_identifier.casefold())
        password_matches = hmac.compare_digest(_password_hash(password or "", salt), expected)
        if not identifier_matches or not password_matches:
            raise AuthenticationError("That password did not match.")
        token = _new_session(account)
        return token, _public_account(account)


def update_profile(name: str, identifier: str) -> dict:
    name, identifier = _validate_profile(name, identifier)
    with _lock:
        account = _load_account()
        if account is None:
            raise AuthenticationError("No local account exists.")
        account["name"] = name
        account["identifier"] = identifier
        _save_account(account)
        return _public_account(account)


def change_password(current_password: str, new_password: str) -> str:
    if len(new_password or "") < 10 or len(new_password) > 1024:
        raise AuthenticationError("Use a new password between 10 and 1024 characters.")
    if len(current_password or "") > 1024:
        raise AuthenticationError("Current password did not match.")
    with _lock:
        account = _load_account()
        if account is None:
            raise AuthenticationError("No local account exists.")
        try:
            salt = bytes.fromhex(account["salt"])
            expected = bytes.fromhex(account["password_hash"])
        except (KeyError, ValueError):
            raise AuthenticationError("The local account file is invalid. Remove it and create a new account.")
        actual = _password_hash(current_password or "", salt)
        if not hmac.compare_digest(actual, expected):
            raise AuthenticationError("Current password did not match.")
        new_salt = secrets.token_bytes(_salt_bytes)
        account["salt"] = new_salt.hex()
        account["password_hash"] = _password_hash(new_password, new_salt).hex()
        _save_account(account)
    return "Password updated."


def _new_session(account: dict) -> str:
    expires_at = int(time.time()) + SESSION_TTL_SECONDS
    nonce = secrets.token_urlsafe(24)
    payload = f"{expires_at}.{nonce}"
    signature = hmac.new(bytes.fromhex(_ensure_session_secret(account)), payload.encode(), hashlib.sha256).hexdigest()
    token = f"{payload}.{signature}"
    _sessions.add(token)
    return token


def is_authenticated(token: str) -> bool:
    if not token:
        return False
    with _lock:
        try:
            expires_text, nonce, signature = token.split(".", 2)
            expires_at = int(expires_text)
            if expires_at <= int(time.time()) or len(nonce) < 16:
                return False
            account = _load_account()
            if account is None:
                return False
            secret = bytes.fromhex(_ensure_session_secret(account))
            payload = f"{expires_text}.{nonce}"
            expected = hmac.new(secret, payload.encode(), hashlib.sha256).hexdigest()
            return hmac.compare_digest(signature, expected)
        except (ValueError, TypeError):
            return False


def logout(token: str):
    with _lock:
        if not is_authenticated(token):
            return
        account = _load_account()
        if account is not None:
            account["session_secret"] = secrets.token_hex(32)
            _save_account(account)
        _sessions.clear()
