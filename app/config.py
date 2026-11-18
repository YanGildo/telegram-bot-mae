"""Configuração lida do arquivo .env."""

import re
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

TOKEN_RE = re.compile(r"^\d{5,}:[A-Za-z0-9_-]{30,}$")


def _parse_ids(raw: str) -> list[int]:
    return [int(p) for p in re.split(r"[,\s;]+", raw.strip()) if p]


class Settings(BaseSettings):
    # utf-8-sig aceita .env salvo pelo Bloco de Notas com BOM
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8-sig", extra="ignore")

    bot_token: SecretStr  # token do bot mãe
    admin_ids: str = ""  # donos da plataforma (você)
    secret_key: SecretStr | None = None  # chave que criptografa os tokens dos bots filhos
    database_url: str = "sqlite+aiosqlite:///dados/mae.db"
    log_level: str = "INFO"
    timezone: str = "America/Sao_Paulo"

    @field_validator("bot_token")
    @classmethod
    def _check_token(cls, v: SecretStr) -> SecretStr:
        if not TOKEN_RE.match(v.get_secret_value().strip()):
            raise ValueError("BOT_TOKEN vazio ou com formato inválido")
        return SecretStr(v.get_secret_value().strip())

    @field_validator("admin_ids")
    @classmethod
    def _check_admins(cls, v: str) -> str:
        try:
            _parse_ids(v)
        except ValueError:
            raise ValueError("ADMIN_IDS deve ter só números separados por vírgula")
        return v

    @field_validator("timezone")
    @classmethod
    def _check_tz(cls, v: str) -> str:
        try:
            ZoneInfo(v.strip())
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError("TIMEZONE inválido (ex.: America/Sao_Paulo)")
        return v.strip()

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    @property
    def owners(self) -> frozenset[int]:
        return frozenset(_parse_ids(self.admin_ids))

    def is_owner(self, user_id: int | None) -> bool:
        return user_id is not None and user_id in self.owners


def ensure_secret_key(settings: Settings, env_path: Path = Path(".env")) -> Settings:
    """Na primeira vez, cria a SECRET_KEY e grava no .env. Sem ela, os tokens salvos não abrem."""
    if settings.secret_key and settings.secret_key.get_secret_value().strip():
        return settings
    from cryptography.fernet import Fernet
    key = Fernet.generate_key().decode()
    text = env_path.read_text(encoding="utf-8-sig") if env_path.exists() else ""
    if text and not text.endswith("\n"):
        text += "\n"
    text += ("# Chave que protege os tokens dos bots filhos. NÃO apague nem troque:\n"
             "# sem ela, todos os bots filhos precisam ser cadastrados de novo.\n"
             f"SECRET_KEY={key}\n")
    env_path.write_text(text, encoding="utf-8")
    return settings.model_copy(update={"secret_key": SecretStr(key)})


def load_settings() -> Settings:
    try:
        settings = Settings()
    except Exception as e:  # mensagem simples em vez do erro gigante do pydantic
        raise SystemExit(f"Erro no .env: {e}\nConfira o arquivo .env (veja o .env.exemplo).")
    return ensure_secret_key(settings)
