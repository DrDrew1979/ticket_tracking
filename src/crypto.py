"""Шифрование персональных данных пользователей (репозиторий публичный!).

В таблице users хранятся:
  - uid   = HMAC-SHA256(chat_id, DB_KEY)[:16]  — детерминированный ключ
            для быстрого поиска по входящему сообщению;
  - ident_enc / tracking_enc / dialog_enc = Fernet(JSON) — обратимо.

DB_KEY задаётся секретом репозитория. Без него мультипользовательский
режим отключается, личный мониторинг владельца работает как раньше.

Генерация ключа:
  python -c "from src.crypto import gen_key; print(gen_key())"
"""
import base64
import hashlib
import hmac
import json
import os

from cryptography.fernet import Fernet, InvalidToken


def gen_key() -> str:
    return base64.urlsafe_b64encode(os.urandom(32)).decode()


class Crypto:
    def __init__(self, key: str):
        key = key.strip()
        self._key = key
        self._f = Fernet(key.encode())

    # ---------- обратимое шифрование ----------
    def enc(self, obj) -> str:
        raw = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        return self._f.encrypt(raw).decode("ascii")

    def dec(self, s: str):
        if not s:
            return None
        try:
            return json.loads(self._f.decrypt(s.encode("ascii")))
        except (InvalidToken, ValueError, TypeError):
            return None

    # ---------- детерминированный поиск ----------
    def uid(self, chat_id) -> str:
        mac = hmac.new(self._key.encode(), str(chat_id).encode("utf-8"),
                       hashlib.sha256).hexdigest()
        return mac[:16]
