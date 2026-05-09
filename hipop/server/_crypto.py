"""租户凭据对称加密（Fernet）。

key 派生自 JWT_SECRET（生产 export 固定），失败时随机 + 警告。
加密值前缀 "fer:" 标识，方便以后换算法。
"""
from __future__ import annotations

import os
import base64
import hashlib
import warnings
from typing import Optional

from cryptography.fernet import Fernet, InvalidToken


def _derive_key() -> bytes:
    """从 JWT_SECRET 派生 32 字节 fernet key。"""
    seed = os.environ.get("JWT_SECRET") or "hipop_dev_unstable_key"
    if seed == "hipop_dev_unstable_key":
        warnings.warn("JWT_SECRET 未设：派生的加密 key 是 dev 默认值，重启可能解不出旧密文")
    h = hashlib.sha256(seed.encode("utf-8")).digest()
    return base64.urlsafe_b64encode(h)


_FERNET: Optional[Fernet] = None


def _get_fernet() -> Fernet:
    global _FERNET
    if _FERNET is None:
        _FERNET = Fernet(_derive_key())
    return _FERNET


def encrypt(plain: Optional[str]) -> Optional[str]:
    if plain is None or plain == "":
        return None
    token = _get_fernet().encrypt(plain.encode("utf-8")).decode("ascii")
    return "fer:" + token


def decrypt(ciphertext: Optional[str]) -> Optional[str]:
    if not ciphertext:
        return None
    if ciphertext.startswith("fer:"):
        ciphertext = ciphertext[4:]
    try:
        return _get_fernet().decrypt(ciphertext.encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError):
        return None
