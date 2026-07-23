import hashlib
import json
import logging
from typing import Optional

from cryptography.fernet import Fernet

from backend.util.settings import Settings

logger = logging.getLogger(__name__)

ENCRYPTION_KEY = Settings().secrets.encryption_key

_FERNET_KEYGEN_HINT = (
    'python -c "from cryptography.fernet import Fernet; '
    'print(Fernet.generate_key().decode())"'
)


def key_fingerprint(key: str | bytes) -> str:
    """Short, non-secret fingerprint (first 12 sha256 hex chars) of a key.

    Logged at startup so an ``ENCRYPTION_KEY`` mismatch across services — which
    otherwise surfaces only as an opaque decrypt failure at runtime — is caught
    by confirming every service prints the same fingerprint. Safe to log: a
    truncated hash reveals nothing usable about the key.
    """
    raw = key.encode() if isinstance(key, str) else key
    return hashlib.sha256(raw).hexdigest()[:12]


# Fail fast at startup on a malformed key (e.g. a Render `generateValue` secret
# that isn't valid url-safe-base64) with an actionable message, instead of a
# cryptic `InvalidToken` on the first credential decrypt. Empty key is allowed
# here (unset in local/dev); JSONCryptor still errors if used without one.
if ENCRYPTION_KEY:
    try:
        Fernet(ENCRYPTION_KEY.encode())
    except Exception as e:
        raise ValueError(
            "ENCRYPTION_KEY is not a valid Fernet key (must be 32 url-safe "
            f"base64-encoded bytes). Generate one with: {_FERNET_KEYGEN_HINT}"
        ) from e
    logger.info(
        f"ENCRYPTION_KEY loaded (fingerprint={key_fingerprint(ENCRYPTION_KEY)})"
    )


class JSONCryptor:
    def __init__(self, key: Optional[str] = None):
        # Use provided key or get from environment
        self.key = key or ENCRYPTION_KEY
        if not self.key:
            raise ValueError(
                "Encryption key must be provided or set in ENCRYPTION_KEY environment variable"
            )
        self.fernet = Fernet(
            self.key.encode() if isinstance(self.key, str) else self.key
        )

    def encrypt(self, data: dict) -> str:
        """Encrypt dictionary data to string"""
        json_str = json.dumps(data)
        encrypted = self.fernet.encrypt(json_str.encode())
        return encrypted.decode()

    def decrypt(self, encrypted_str: str) -> dict:
        """Decrypt string to dictionary"""
        if not encrypted_str:
            return {}
        try:
            decrypted = self.fernet.decrypt(encrypted_str.encode())
            return json.loads(decrypted.decode())
        except Exception:
            return {}
