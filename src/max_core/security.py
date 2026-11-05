"""Single-owner temporary bearer authentication; NOT remote production identity."""

from __future__ import annotations

from hmac import compare_digest

from fastapi import HTTPException, Request, status


class BootstrapBearerAuth:
    """Reject missing, malformed, duplicate, and incorrect Authorization headers."""

    def __init__(self, token: str) -> None:
        self._secret = token.encode("utf-8")

    def __call__(self, request: Request) -> str:
        values = [
            value for key, value in request.scope["headers"] if key.lower() == b"authorization"
        ]
        if len(values) != 1:
            self._deny()
        header = values[0].decode("latin-1")
        scheme, separator, credential = header.partition(" ")
        if (
            scheme.lower() != "bearer"
            or separator != " "
            or not credential
            or any(char.isspace() for char in credential)
            or not compare_digest(credential.encode("utf-8"), self._secret)
        ):
            self._deny()
        return "owner"

    @staticmethod
    def _deny() -> None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Unauthorized",
            headers={"WWW-Authenticate": "Bearer"},
        )
