"""Ed25519 signature checks and small encoding helpers.

On the desktop the backend is PyNaCl (python3-nacl from apt, libsodium).
If it is missing, python3-cryptography (OpenSSL) is used instead; both do
strict RFC 8032 verification. There is deliberately no pure-Python
fallback here: if neither library loads, the gateway refuses to start.
"""
import base64
import binascii

BACKEND = None
_verify = None

try:  # the desktop's choice
    from nacl.signing import VerifyKey as _NaclVerifyKey
    from nacl.exceptions import BadSignatureError as _NaclBad

    def _verify_nacl(pub, msg, sig):
        try:
            _NaclVerifyKey(pub).verify(msg, sig)
            return True
        except (_NaclBad, ValueError, TypeError):
            return False

    _verify = _verify_nacl
    BACKEND = "pynacl"
except ImportError:
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import (
            Ed25519PublicKey as _CPub,
        )
        from cryptography.exceptions import InvalidSignature as _CBad

        def _verify_crypto(pub, msg, sig):
            try:
                _CPub.from_public_bytes(pub).verify(sig, msg)
                return True
            except (_CBad, ValueError, TypeError):
                return False

        _verify = _verify_crypto
        BACKEND = "cryptography"
    except ImportError:
        pass


def available():
    return _verify is not None


def ed25519_verify(pub, msg, sig):
    """True only for a valid signature by the 32-byte key `pub`."""
    if _verify is None:
        raise RuntimeError("no Ed25519 backend: install python3-nacl")
    if not isinstance(pub, bytes) or len(pub) != 32:
        return False
    if not isinstance(sig, bytes) or len(sig) != 64:
        return False
    return _verify(pub, msg, sig)


def b64url_decode(s):
    """Base64url, padding optional. Raises ValueError on anything else."""
    if not isinstance(s, str) or not s or len(s) > 8192:
        raise ValueError("empty or oversized base64")
    if any(c not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_=" for c in s):
        raise ValueError("not base64url")
    s = s.rstrip("=")
    try:
        return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))
    except (binascii.Error, ValueError):
        raise ValueError("bad base64url")


def b64url_encode(b):
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode("ascii")
