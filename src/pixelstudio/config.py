"""개인 Codex 설정과 분리된 프로젝트 설정."""

from dataclasses import dataclass, field
import os
from pathlib import Path

from dotenv import dotenv_values


PROJECT_ROOT = Path(__file__).resolve().parents[2]
PROVIDER_KEYS = {"openai": "OPENAI_API_KEY", "anthropic": "ANTHROPIC_API_KEY",
                 "google_genai": "GOOGLE_API_KEY"}


class ConfigurationError(ValueError):
    pass


@dataclass(frozen=True)
class Settings:
    root: Path
    api_key: str = field(default="", repr=False)
    model: str = ""
    provider: str = "openai"
    bot_token: str = field(default="", repr=False)
    guild_id: str = ""
    owner_id: str = ""
    allowed_channel_id: str = ""
    mcp_package: str = "@pebbly/aseprite-ai-artist@0.8.2"
    mcp_timeout: int = 30
    job_timeout: int = 480
    max_rounds: int = 16
    max_tool_calls: int = 32
    max_output_tokens: int = 6000
    max_model_calls: int = 40
    max_revisions: int = 2

    @classmethod
    def load(cls, root: Path | None = None) -> "Settings":
        root = (root or PROJECT_ROOT).resolve()
        values = dotenv_values(root / ".env", interpolate=False)

        def value(name: str, default: str = "") -> str:
            return str(os.environ.get(name, values.get(name) or default)).strip()

        def integer(name: str, default: int, minimum: int = 1) -> int:
            raw = value(name, str(default))
            if not raw.isdecimal() or int(raw) < minimum:
                raise ConfigurationError(f"{name}은 {minimum} 이상의 정수여야 합니다.")
            return int(raw)

        provider = value("MODEL_PROVIDER", "openai").lower()
        if provider not in PROVIDER_KEYS:
            raise ConfigurationError("MODEL_PROVIDER는 openai, anthropic, google_genai 중 하나여야 합니다.")
        return cls(
            root=root,
            provider=provider,
            api_key=value(PROVIDER_KEYS[provider]),
            model=value("MODEL_NAME") or (value("OPENAI_MODEL") if provider == "openai" else ""),
            bot_token=value("DISCORD_BOT_TOKEN"),
            guild_id=value("DISCORD_GUILD_ID"),
            owner_id=value("DISCORD_OWNER_ID"),
            allowed_channel_id=value("DISCORD_ALLOWED_CHANNEL_ID"),
            mcp_package=value("ASEPRITE_MCP_PACKAGE", "@pebbly/aseprite-ai-artist@0.8.2"),
            mcp_timeout=integer("MCP_TIMEOUT_SECONDS", 30),
            job_timeout=integer("JOB_TIMEOUT_SECONDS", 480),
            max_rounds=integer("MAX_AGENT_ROUNDS", 16),
            max_tool_calls=integer("MAX_TOOL_CALLS", 32),
            max_output_tokens=integer("MAX_OUTPUT_TOKENS", 6000),
            max_model_calls=integer("MAX_MODEL_CALLS", 40),
            max_revisions=integer("MAX_REVISIONS", 2, minimum=0),
        )

    def require_api(self) -> None:
        if self.provider not in PROVIDER_KEYS:
            raise ConfigurationError("지원하지 않는 MODEL_PROVIDER입니다.")
        missing = [name for name, value in (
            (PROVIDER_KEYS[self.provider], self.api_key), ("MODEL_NAME (OpenAI는 OPENAI_MODEL도 가능)", self.model)
        ) if not value]
        if missing:
            raise ConfigurationError(".env에 다음 설정이 필요합니다: " + ", ".join(missing))

    def require_discord(self) -> None:
        if not self.bot_token:
            raise ConfigurationError(".env에 DISCORD_BOT_TOKEN을 설정해주세요.")
        for name, value in (("DISCORD_GUILD_ID", self.guild_id),
                            ("DISCORD_OWNER_ID", self.owner_id)):
            if not value.isdecimal():
                raise ConfigurationError(f"{name}를 숫자로 설정해주세요.")
        if self.allowed_channel_id and not self.allowed_channel_id.isdecimal():
            raise ConfigurationError("DISCORD_ALLOWED_CHANNEL_ID를 숫자로 설정해주세요.")
