"""Passkeys (WebAuthn) without a big library: just enough to register a passkey and check a sign-in.

Supports the two signature types every phone and computer uses (ES256 and RS256) and "none" attestation,
which is what browsers send by default. Everything is checked against the website's own address.
"""
from __future__ import annotations

import base64
import hashlib
import json
import struct

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa


class PasskeyError(Exception):
    pass


def b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def unb64u(s: str) -> bytes:
    if not isinstance(s, str) or len(s) > 20000:
        raise PasskeyError("bad data")
    try:
        return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))
    except Exception:
        raise PasskeyError("bad data")


def cbor(data: bytes, i: int = 0, depth: int = 0):
    """A small CBOR reader (what attestation objects and COSE keys use). Returns (value, next_index)."""
    if depth > 8 or i >= len(data):
        raise PasskeyError("bad data")
    ib = data[i]
    major, info = ib >> 5, ib & 31
    i += 1
    if info < 24:
        n = info
    elif info in (24, 25, 26, 27):
        size = {24: 1, 25: 2, 26: 4, 27: 8}[info]
        if i + size > len(data):
            raise PasskeyError("bad data")
        n = int.from_bytes(data[i:i + size], "big")
        i += size
    else:
        raise PasskeyError("unsupported")
    if major == 0:
        return n, i
    if major == 1:
        return -1 - n, i
    if major in (2, 3):
        if n > 20000 or i + n > len(data):
            raise PasskeyError("bad data")
        raw = data[i:i + n]
        return (raw if major == 2 else raw.decode("utf-8", "replace")), i + n
    if major == 4:
        if n > 64:
            raise PasskeyError("bad data")
        out = []
        for _ in range(n):
            v, i = cbor(data, i, depth + 1)
            out.append(v)
        return out, i
    if major == 5:
        if n > 64:
            raise PasskeyError("bad data")
        d = {}
        for _ in range(n):
            k, i = cbor(data, i, depth + 1)
            v, i = cbor(data, i, depth + 1)
            d[k] = v
        return d, i
    if major == 7 and info in (20, 21, 22):
        return {20: False, 21: True, 22: None}[info], i
    raise PasskeyError("unsupported")


def _client_data(raw: bytes, kind: str, challenge: str, origin: str) -> None:
    try:
        cd = json.loads(raw.decode())
    except Exception:
        raise PasskeyError("bad data")
    if not isinstance(cd, dict) or cd.get("type") != kind or cd.get("challenge") != challenge or cd.get("origin") != origin:
        raise PasskeyError("This sign-in doesn't match this website.")
    if cd.get("crossOrigin"):
        raise PasskeyError("This sign-in doesn't match this website.")


def _check_flags(auth: bytes, rp_id: str, need_attested: bool) -> int:
    if len(auth) < 37 or auth[:32] != hashlib.sha256(rp_id.encode()).digest():
        raise PasskeyError("This sign-in doesn't match this website.")
    flags = auth[32]
    if not flags & 0x01 or not flags & 0x04:  # the person must be there (UP) and verified by Face ID / fingerprint / PIN (UV)
        raise PasskeyError("Your device didn't confirm it's you. Please try again.")
    if need_attested and not flags & 0x40:
        raise PasskeyError("bad data")
    return struct.unpack(">I", auth[33:37])[0]


def verify_registration(client_data: str, attestation: str, challenge: str, origin: str, rp_id: str) -> dict:
    """Check a new passkey. Returns {"id": credential id (b64url), "key": public key (json-friendly), "count": sign count}."""
    _client_data(unb64u(client_data), "webauthn.create", challenge, origin)
    att, _ = cbor(unb64u(attestation))
    if not isinstance(att, dict) or not isinstance(att.get("authData"), bytes):
        raise PasskeyError("bad data")
    auth = att["authData"]
    count = _check_flags(auth, rp_id, True)
    if len(auth) < 55:
        raise PasskeyError("bad data")
    n = struct.unpack(">H", auth[53:55])[0]
    cred_id = auth[55:55 + n]
    if not 16 <= len(cred_id) <= 1023 or len(auth) < 55 + n:
        raise PasskeyError("bad data")
    cose, _ = cbor(auth, 55 + n)
    if not isinstance(cose, dict):
        raise PasskeyError("bad data")
    alg = cose.get(3)
    if alg == -7 and cose.get(1) == 2 and cose.get(-1) == 1 and isinstance(cose.get(-2), bytes) and isinstance(cose.get(-3), bytes):
        key = {"alg": -7, "x": b64u(cose[-2]), "y": b64u(cose[-3])}
        _load(key)  # (is it a real point on the curve?)
    elif alg == -257 and cose.get(1) == 3 and isinstance(cose.get(-1), bytes) and isinstance(cose.get(-2), bytes):
        key = {"alg": -257, "n": b64u(cose[-1]), "e": b64u(cose[-2])}
        _load(key)
    else:
        raise PasskeyError("This device uses a kind of passkey this website can't use.")
    return {"id": b64u(cred_id), "key": key, "count": count}


def _load(key: dict):
    try:
        if key["alg"] == -7:
            return ec.EllipticCurvePublicNumbers(int.from_bytes(unb64u(key["x"]), "big"), int.from_bytes(unb64u(key["y"]), "big"), ec.SECP256R1()).public_key()
        return rsa.RSAPublicNumbers(int.from_bytes(unb64u(key["e"]), "big"), int.from_bytes(unb64u(key["n"]), "big")).public_key()
    except PasskeyError:
        raise
    except Exception:
        raise PasskeyError("bad data")


def verify_assertion(key: dict, old_count: int, client_data: str, authenticator_data: str, signature: str, challenge: str, origin: str, rp_id: str) -> int:
    """Check a passkey sign-in. Returns the new sign count."""
    raw_cd = unb64u(client_data)
    _client_data(raw_cd, "webauthn.get", challenge, origin)
    auth = unb64u(authenticator_data)
    count = _check_flags(auth, rp_id, False)
    if old_count and count and count <= old_count:
        raise PasskeyError("This passkey looks copied, so it was refused.")
    signed = auth + hashlib.sha256(raw_cd).digest()
    sig = unb64u(signature)
    pub = _load(key)
    try:
        if key["alg"] == -7:
            pub.verify(sig, signed, ec.ECDSA(hashes.SHA256()))
        else:
            pub.verify(sig, signed, padding.PKCS1v15(), hashes.SHA256())
    except InvalidSignature:
        raise PasskeyError("That passkey didn't check out.")
    return max(count, old_count)
