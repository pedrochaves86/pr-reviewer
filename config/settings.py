"""
Configurações centrais — lidas de variáveis de ambiente ou ficheiro .env
"""

import os
from dataclasses import dataclass, field
from dotenv import load_dotenv

load_dotenv()


@dataclass
class Settings:
    # ── Entrada direta (obrigatória) ───────────────────────────────────────
    # Lista de PR URLs separados por vírgula.
    github_pr_url: str = field(default_factory=lambda: os.getenv("GITHUB_PR_URL", ""))

    # ── GitHub ─────────────────────────────────────────────────────────────
    github_token: str = field(default_factory=lambda: os.getenv("GITHUB_TOKEN", ""))
    github_reviewer_login: str = field(default_factory=lambda: os.getenv("GITHUB_REVIEWER_LOGIN", ""))

    # ── GitHub Models (via licença GitHub/Copilot) ────────────────────────
    github_models_token: str = field(default_factory=lambda: os.getenv("GITHUB_MODELS_TOKEN", ""))
    github_models_endpoint: str = field(
        default_factory=lambda: (
            os.getenv("GITHUB_MODELS_ENDPOINT", "")
            or "https://models.inference.ai.azure.com/chat/completions"
        )
    )
    github_models_catalog_endpoint: str = field(
        default_factory=lambda: (
            os.getenv("GITHUB_MODELS_CATALOG_ENDPOINT", "")
            or "https://models.inference.ai.azure.com/models"
        )
    )
    # Modelo validado no catálogo GitHub Models acessível no tenant EDP.
    github_models_model: str = field(
        default_factory=lambda: (os.getenv("GITHUB_MODELS_MODEL", "") or "gpt-4o")
    )

    # ── SonarQube (opcional) ───────────────────────────────────────────────
    sonar_url: str = field(default_factory=lambda: os.getenv("SONAR_URL", ""))
    sonar_token: str = field(default_factory=lambda: os.getenv("SONAR_TOKEN", ""))

    # ── Checkmarx (opcional) ───────────────────────────────────────────────
    checkmarx_url: str = field(default_factory=lambda: os.getenv("CHECKMARX_URL", ""))
    checkmarx_tenant: str = field(default_factory=lambda: os.getenv("CHECKMARX_TENANT", ""))
    checkmarx_api_key: str = field(default_factory=lambda: os.getenv("CHECKMARX_API_KEY", ""))

    # ── Comportamento ──────────────────────────────────────────────────────
    review_language: str = field(
        default_factory=lambda: os.getenv("REVIEW_LANGUAGE", "pt")
    )

    def validate(self):
        required = {
            "GITHUB_PR_URL": self.github_pr_url,
            "GITHUB_TOKEN": self.github_token,
            "GITHUB_REVIEWER_LOGIN": self.github_reviewer_login,
            "GITHUB_MODELS_TOKEN|GITHUB_TOKEN": self.github_models_token or self.github_token,
        }

        missing = [k for k, v in required.items() if not v]
        if missing:
            raise EnvironmentError(
                f"Variáveis de ambiente obrigatórias em falta: {', '.join(missing)}\n"
                f"Copia o ficheiro .env.example para .env e preenche os valores."
            )
