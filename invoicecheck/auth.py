"""Accounts, passwords, login sessions and login throttling.

Standard library only: scrypt for passwords, random tokens for sessions.
The cookie holds a random token; the database holds its SHA-256, so a copy
of the database does not let anyone log in.
"""
from __future__ import annotations

import hashlib
import hmac
import re
import secrets
from datetime import timedelta

from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from . import config
from .models import Account, LoginFailure, LoginSession, now

SCRYPT_N, SCRYPT_R, SCRYPT_P = 2 ** 14, 8, 1
MAX_PASSWORD_CHARS = 200
EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class AuthError(ValueError):
    """Signing up or logging in was refused. The message is safe to show."""
    status_code = 400


class Throttled(AuthError):
    status_code = 429


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P, dklen=32)
    return f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt, digest = stored.split("$")
        if scheme != "scrypt" or len(password) > MAX_PASSWORD_CHARS:
            return False
        got = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=int(n), r=int(r), p=int(p),
                             dklen=len(digest) // 2)
    except ValueError:
        return False
    return hmac.compare_digest(got.hex(), digest)


_DUMMY_HASH = hash_password(secrets.token_hex(8))   # so an unknown email costs as much time as a wrong password


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def create_account(session: Session, email: str, name: str, password: str, invite_code: str = "") -> Account:
    email, name = (email or "").strip().lower(), (name or "").strip()
    if config.INVITE_CODE and not hmac.compare_digest(invite_code.strip().encode(), config.INVITE_CODE.encode()):
        raise AuthError("That invite code is not right.")
    if not EMAIL.match(email) or len(email) > 254:
        raise AuthError("Enter an email address.")
    if not name or len(name) > 80:
        raise AuthError("Enter your name (up to 80 characters).")
    if not config.MIN_PASSWORD_CHARS <= len(password) <= MAX_PASSWORD_CHARS:
        raise AuthError(f"Use a password of {config.MIN_PASSWORD_CHARS} to {MAX_PASSWORD_CHARS} characters.")
    if session.scalar(select(func.count()).select_from(Account)) >= config.MAX_ACCOUNTS:
        raise AuthError("This demo has reached its limit on accounts.")
    account = Account(email=email, name=name, password_hash=hash_password(password))
    session.add(account)
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        raise AuthError("An account with that email already exists.") from None
    return account


def _failures(session: Session, key: str) -> int:
    since = now() - timedelta(minutes=config.LOGIN_WINDOW_MINUTES)
    return session.scalar(select(func.count()).select_from(LoginFailure)
                          .where(LoginFailure.key == key, LoginFailure.at > since)) or 0


def log_in(session: Session, email: str, password: str, ip: str = "") -> str:
    """Check the password and return a new session token. Too many recent
    failures for the email or the address refuse the attempt before the
    password is looked at, so a correct guess does not get through either."""
    email = (email or "").strip().lower()[:254]
    keys = [(f"email:{email}", config.MAX_LOGIN_FAILURES_PER_EMAIL)]
    if ip:
        keys.append((f"ip:{ip}"[:300], config.MAX_LOGIN_FAILURES_PER_IP))
    if any(_failures(session, key) >= limit for key, limit in keys):
        raise Throttled(f"Too many failed attempts. Try again in {config.LOGIN_WINDOW_MINUTES} minutes.")
    account = session.scalar(select(Account).where(Account.email == email))
    if not verify_password(password or "", account.password_hash if account else _DUMMY_HASH) or account is None:
        session.execute(delete(LoginFailure).where(
            LoginFailure.at < now() - timedelta(minutes=config.LOGIN_WINDOW_MINUTES)))
        session.add_all(LoginFailure(key=key) for key, _ in keys)
        session.commit()
        raise AuthError("That email and password do not match an account.")
    token = secrets.token_urlsafe(32)
    session.execute(delete(LoginSession).where(LoginSession.expires_at < now()))
    session.add(LoginSession(token_hash=_token_hash(token), account_id=account.id,
                             expires_at=now() + timedelta(days=config.SESSION_DAYS)))
    session.commit()
    return token


def account_for_token(session: Session, token: str | None) -> Account | None:
    if not token:
        return None
    return session.scalar(select(Account).join(LoginSession, LoginSession.account_id == Account.id).where(
        LoginSession.token_hash == _token_hash(token), LoginSession.expires_at > now()))


def log_out(session: Session, token: str | None) -> None:
    if token:
        session.execute(delete(LoginSession).where(LoginSession.token_hash == _token_hash(token)))
        session.commit()


def check_password(account: Account, password: str) -> None:
    """Destructive actions ask for the password again."""
    if not verify_password(password or "", account.password_hash):
        raise AuthError("That password is not right, so nothing was deleted.")
