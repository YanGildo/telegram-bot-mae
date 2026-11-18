"""Criptografia dos tokens dos bots filhos e leitura de token em mensagens."""

import re

from cryptography.fernet import Fernet, InvalidToken

TOKEN_IN_TEXT = re.compile(r"\b(\d{6,12}:[A-Za-z0-9_-]{30,50})\b")


class TokenBox:
    def __init__(self, key: str):
        self._f = Fernet(key.encode() if isinstance(key, str) else key)

    def seal(self, token: str) -> str:
        return self._f.encrypt(token.encode()).decode()

    def open(self, sealed: str) -> str | None:
        try:
            return self._f.decrypt(sealed.encode()).decode()
        except (InvalidToken, ValueError):
            return None


def find_token(text: str | None) -> str | None:
    """Acha o token numa mensagem colada ou encaminhada do @BotFather."""
    if not text:
        return None
    m = TOKEN_IN_TEXT.search(text)
    return m.group(1) if m else None


def mask(token: str) -> str:
    bot_id, _, rest = token.partition(":")
    return f"{bot_id}:{rest[:3]}…{rest[-3:]}"
