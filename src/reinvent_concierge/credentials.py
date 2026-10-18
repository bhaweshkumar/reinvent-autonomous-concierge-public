"""Secure credential management for AWS Events API tokens."""

import hashlib
import json
import os
from pathlib import Path
from typing import Any, Dict, Optional

from .exceptions import CredentialsMissingError, TokenStorageError


class CredentialManager:
    """Stores the attendee's OAuth tokens in a file readable only by the owner.

    Token values are never logged or included in exception messages.
    """

    def __init__(self, credentials_path: Path):
        self.credentials_path = Path(credentials_path).expanduser()

    def store_token(self, token: str, refresh_token: Optional[str] = None) -> None:
        """Persist the access token (and optional refresh token) with mode 0600.

        Raises:
            ValueError: If ``token`` is empty
            TokenStorageError: If the file cannot be written
        """
        if not token:
            raise ValueError("token must not be empty")

        payload: Dict[str, str] = {"token": token}
        if refresh_token:
            payload["refresh_token"] = refresh_token

        try:
            self.credentials_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            # Create with 0600 up front so the token is never world-readable, even briefly.
            fd = os.open(self.credentials_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(payload, f)
            # os.open keeps the old mode of a pre-existing file.
            os.chmod(self.credentials_path, 0o600)
        except OSError as e:
            raise TokenStorageError(
                f"Failed to store token at {self.credentials_path}: {e.strerror}"
            ) from e

    def _read(self) -> Dict[str, Any]:
        if not self.credentials_path.exists():
            raise CredentialsMissingError(
                f"No token found at {self.credentials_path}. Run: concierge login"
            )
        try:
            with open(self.credentials_path, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError) as e:
            raise TokenStorageError(
                f"Failed to load credentials from {self.credentials_path}"
            ) from e
        if not isinstance(data, dict):
            raise TokenStorageError(f"Malformed credentials file {self.credentials_path}")
        return data

    def get_token(self) -> str:
        """Return the stored access token.

        Raises:
            CredentialsMissingError: If no token is configured
            TokenStorageError: If the credentials file cannot be read
        """
        token = self._read().get("token")
        if not token:
            raise CredentialsMissingError("Token file exists but contains no token")
        return token

    def get_refresh_token(self) -> Optional[str]:
        """Return the stored refresh token, or None when there is none."""
        try:
            return self._read().get("refresh_token") or None
        except CredentialsMissingError:
            return None

    def get_auth_header(self) -> Dict[str, str]:
        """Return ``{"Authorization": "Bearer <token>"}``.

        Raises:
            CredentialsMissingError: If no token is configured
        """
        return {"Authorization": f"Bearer {self.get_token()}"}

    def hash_token(self) -> str:
        """SHA-256 hex digest of the access token for audit logs; empty if unavailable."""
        try:
            token = self.get_token()
        except (CredentialsMissingError, TokenStorageError):
            return ""
        return hashlib.sha256(token.encode()).hexdigest()

    def token_exists(self) -> bool:
        """Return True if a readable, non-empty token is stored."""
        try:
            self.get_token()
            return True
        except (CredentialsMissingError, TokenStorageError):
            return False

    def clear(self) -> None:
        """Delete the stored tokens."""
        self.credentials_path.unlink(missing_ok=True)
