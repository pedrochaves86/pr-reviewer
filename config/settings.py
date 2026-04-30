"""
Configurações centrais — lidas de variáveis de ambiente ou ficheiro .env
"""

import os
from dataclasses import dataclass, field
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()


@dataclass
class Settings:
    # ── Entrada direta (opcional) ──────────────────────────────────────────
    # Se definido, processa PR(s) diretamente sem ler Teams.
    github_pr_url: str = field(default_factory=lambda: os.getenv("GITHUB_PR_URL", ""))

    # ── Teams ──────────────────────────────────────────────────────────────
    # Graph API (recomendado) OU Incoming Webhook para envio
    teams_tenant_id: str = field(default_factory=lambda: os.getenv("TEAMS_TENANT_ID", ""))
    teams_client_id: str = field(default_factory=lambda: os.getenv("TEAMS_CLIENT_ID", ""))
    teams_client_secret: str = field(default_factory=lambda: os.getenv("TEAMS_CLIENT_SECRET", ""))
    teams_team_id: str = field(default_factory=lambda: os.getenv("TEAMS_TEAM_ID", ""))
    teams_channel_id: str = field(default_factory=lambda: os.getenv("TEAMS_CHANNEL_ID", ""))

    # ── GitHub ─────────────────────────────────────────────────────────────
    github_token: str = field(default_factory=lambda: os.getenv("GITHUB_TOKEN", ""))
    github_reviewer_login: str = field(default_factory=lambda: os.getenv("GITHUB_REVIEWER_LOGIN", ""))

    # ── Anthropic (Claude) ─────────────────────────────────────────────────
    anthropic_api_key: str = field(default_factory=lambda: os.getenv("ANTHROPIC_API_KEY", ""))
    claude_model: str = field(
        default_factory=lambda: (os.getenv("CLAUDE_MODEL", "") or "claude-sonnet-4-6")
    )
    # Modelos alternativos tentados em sequência se o principal falhar (separados por vírgula)
    claude_fallback_models: list[str] = field(
        default_factory=lambda: [
            m.strip()
            for m in (os.getenv("CLAUDE_FALLBACK_MODELS", "") or "").split(",")
            if m.strip()
        ] or ["claude-3-5-sonnet-20241022", "claude-3-5-haiku-20241022"]
    )

    # ── OpenAI (fallback quando Anthropic sem créditos) ───────────────────
    # https://platform.openai.com/api-keys
    openai_api_key: str = field(default_factory=lambda: os.getenv("OPENAI_API_KEY", ""))
    openai_model: str = field(
        default_factory=lambda: (os.getenv("OPENAI_MODEL", "") or "gpt-4o")
    )

    # ── SonarQube (opcional) ───────────────────────────────────────────────
    sonar_url: str = field(default_factory=lambda: os.getenv("SONAR_URL", ""))
    sonar_token: str = field(default_factory=lambda: os.getenv("SONAR_TOKEN", ""))

    # ── Checkmarx (opcional) ───────────────────────────────────────────────
    checkmarx_url: str = field(default_factory=lambda: os.getenv("CHECKMARX_URL", ""))
    checkmarx_tenant: str = field(default_factory=lambda: os.getenv("CHECKMARX_TENANT", ""))
    checkmarx_api_key: str = field(default_factory=lambda: os.getenv("CHECKMARX_API_KEY", ""))

    # ── Comportamento ──────────────────────────────────────────────────────
    poll_interval_minutes: int = field(
        default_factory=lambda: int(os.getenv("POLL_INTERVAL_MINUTES", "5"))
    )
    # Ficheiro para guardar PRs já processados (evita duplicados)
    processed_prs_file: str = field(
        default_factory=lambda: os.getenv("PROCESSED_PRS_FILE", "processed_prs.json")
    )
    # Idioma dos comentários de review
    review_language: str = field(
        default_factory=lambda: os.getenv("REVIEW_LANGUAGE", "pt")
    )

    def validate(self):
        required = {
            "GITHUB_TOKEN": self.github_token,
            "GITHUB_REVIEWER_LOGIN": self.github_reviewer_login,
            "ANTHROPIC_API_KEY": self.anthropic_api_key,
        }

        # Em modo direto (GITHUB_PR_URL definido), Teams é opcional.
        if not (self.github_pr_url or "").strip():
            required.update(
                {
                    "TEAMS_TENANT_ID": self.teams_tenant_id,
                    "TEAMS_CLIENT_ID": self.teams_client_id,
                    "TEAMS_CLIENT_SECRET": self.teams_client_secret,
                    "TEAMS_CHANNEL_ID": self.teams_channel_id,
                }
            )

            channel_value = (self.teams_channel_id or "").strip()
            is_chat_mode = "@thread.v2" in channel_value or "/l/chat/" in channel_value
            if not is_chat_mode:
                required["TEAMS_TEAM_ID"] = self.teams_team_id

        missing = [k for k, v in required.items() if not v]
        if missing:
            raise EnvironmentError(
                f"Variáveis de ambiente obrigatórias em falta: {', '.join(missing)}\n"
                f"Copia o ficheiro .env.example para .env e preenche os valores."
            )
