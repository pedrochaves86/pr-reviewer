"""
Analisador de código via GitHub Copilot API.
Recebe diff + metadados e devolve uma review estruturada.
"""

import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Optional

import requests
from openai import OpenAI, AuthenticationError, PermissionDeniedError

from config.settings import Settings

log = logging.getLogger(__name__)

MAX_DIFF_CHARS = 20_000  # ~5k tokens

COPILOT_API_BASE = "https://api.githubcopilot.com"
COPILOT_TOKEN_URL = "https://api.github.com/copilot_internal/v2/token"


@dataclass
class ReviewResult:
    summary: str  # Resumo executivo
    verdict: str  # "APPROVE" | "REQUEST_CHANGES" | "COMMENT"
    severity: str  # "critical" | "major" | "minor" | "clean"
    issues: list[dict] = field(default_factory=list)
    # Cada issue: { file, line_hint, category, severity, message, suggestion }
    general_comments: list[str] = field(default_factory=list)
    raw_json: dict = field(default_factory=dict)


SYSTEM_PROMPT = """És um Tech Lead sénior a fazer code review de um Pull Request.
Devolve **apenas** um objeto JSON válido (sem markdown, sem texto extra).

Estrutura obrigatória do JSON:
{
  "summary": "<resumo executivo em 2-3 frases>",
  "verdict": "APPROVE" | "REQUEST_CHANGES" | "COMMENT",
  "severity": "clean" | "minor" | "major" | "critical",
  "issues": [
    {
      "file": "<caminho do ficheiro>",
      "line_hint": "<nº linha aproximado ou null>",
      "category": "bug" | "security" | "performance" | "maintainability" | "style" | "sonar" | "checkmarx",
      "severity": "critical" | "major" | "minor",
      "message": "<descrição clara do problema>",
      "suggestion": "<sugestão de melhoria com exemplo antes/depois no formato:\\n**Antes:**\\n```\\n<código atual>\\n```\\n**Depois:**\\n```\\n<código sugerido>\\n```"
    }
  ],
  "general_comments": ["<comentário geral 1>", "..."]
}

Regras:
- Foca-te apenas nos problemas mais relevantes — não reportes issues estilísticos menores nem redundâncias óbvias
- Máximo de 8 issues; prioriza bugs, segurança e problemas de lógica
- Se não há problemas sérios → verdict = "APPROVE"
- Se há problemas que impedem merge → verdict = "REQUEST_CHANGES"
- Se só há sugestões → verdict = "COMMENT"
- Escreve os comentários no idioma configurado
- Sê direto e pragmático; o campo "suggestion" deve sempre mostrar o código antes e depois para facilitar a leitura
- Os resultados de Sonar e Checkmarx já estão filtrados para os ficheiros e linhas tocadas neste PR — reporta apenas esses
- Não inventes problemas fora do diff fornecido
"""

class AIAnalyzer:
    def __init__(self, settings: Settings):
        self.model = settings.copilot_model
        self.language = settings.review_language
        if not settings.github_token:
            raise EnvironmentError(
                "GITHUB_TOKEN não configurado. "
                "É necessário para autenticar na GitHub Copilot API."
            )
        self._github_token = settings.github_token
        self._copilot_token: str | None = None
        self._copilot_token_expires_at: float = 0.0

    def _get_copilot_token(self) -> str:
        """Troca o GitHub PAT por um token de curta duração da Copilot API."""
        now = time.time()
        if self._copilot_token and now < self._copilot_token_expires_at - 60:
            return self._copilot_token

        resp = requests.post(
            COPILOT_TOKEN_URL,
            headers={
                "Authorization": f"token {self._github_token}",
                "Accept": "application/json",
                "Editor-Version": "vscode/1.99.0",
                "Editor-Plugin-Version": "copilot/1.155.0",
                "User-Agent": "GithubCopilot/1.155.0",
            },
            timeout=15,
        )
        if resp.status_code == 401:
            raise RuntimeError(
                "GITHUB_TOKEN inválido ou expirado. Verifica o token no ficheiro .env."
            )
        if resp.status_code == 403:
            raise RuntimeError(
                "A conta não tem acesso ao GitHub Copilot. "
                "Confirma que tens uma subscrição Copilot ativa em https://github.com/settings/copilot"
            )
        resp.raise_for_status()

        data = resp.json()
        self._copilot_token = data["token"]
        self._copilot_token_expires_at = data.get("expires_at", now + 1800)
        log.debug("Copilot token obtido com sucesso.")
        return self._copilot_token

    def _call_copilot(self, user_msg: str) -> str:
        log.info(f"A enviar diff para GitHub Copilot API (modelo: {self.model})...")
        copilot_token = self._get_copilot_token()
        client = OpenAI(base_url=COPILOT_API_BASE, api_key=copilot_token)
        try:
            response = client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": user_msg},
                ],
                max_tokens=4096,
            )
        except AuthenticationError as exc:
            raise RuntimeError(
                "Token Copilot inválido. Tenta novamente — o token de sessão pode ter expirado."
            ) from exc
        except PermissionDeniedError as exc:
            raise RuntimeError(
                "A conta não tem acesso ao GitHub Copilot. "
                "Confirma que tens uma subscrição Copilot ativa."
            ) from exc

        return response.choices[0].message.content or ""

    @staticmethod
    def _changed_lines(diff: str) -> dict[str, set[int]]:
        """Extrai, por ficheiro, as linhas novas tocadas no diff unificado."""
        changed_lines: dict[str, set[int]] = {}
        current_file: Optional[str] = None
        next_line_number: Optional[int] = None

        for line in diff.splitlines():
            if line.startswith("+++ b/"):
                path = line[6:].strip()
                if path == "/dev/null":
                    current_file = None
                    next_line_number = None
                    continue

                current_file = path
                changed_lines.setdefault(current_file, set())
                next_line_number = None
                continue

            if not current_file:
                continue

            if line.startswith("@@"):
                match = re.match(r"@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@", line)
                next_line_number = int(match.group(1)) if match else None
                continue

            if next_line_number is None:
                continue

            if line.startswith("+") and not line.startswith("+++"):
                changed_lines[current_file].add(next_line_number)
                next_line_number += 1
            elif line.startswith("-") and not line.startswith("---"):
                continue
            else:
                next_line_number += 1

        return changed_lines

    @staticmethod
    def _matches_changed_file(target_path: str, changed_lines: dict[str, set[int]]) -> Optional[str]:
        normalized_target = (target_path or "").replace("\\", "/")
        for changed_path in changed_lines:
            if normalized_target.endswith(changed_path):
                return changed_path
        return None

    @staticmethod
    def _filter_sonar(findings: Optional[dict], changed_lines: dict[str, set[int]]) -> Optional[dict]:
        if not findings:
            return None
        filtered = [
            i for i in findings.get("issues", [])
            if (
                (matched_path := AIAnalyzer._matches_changed_file(i.get("component") or "", changed_lines))
                and i.get("line") in changed_lines.get(matched_path, set())
            )
        ]
        if not filtered:
            return None
        return {"total": len(filtered), "issues": filtered}

    @staticmethod
    def _filter_checkmarx(findings: Optional[dict], changed_lines: dict[str, set[int]]) -> Optional[dict]:
        if not findings:
            return None
        filtered = [
            v for v in findings.get("top_vulnerabilities", [])
            if (
                (matched_path := AIAnalyzer._matches_changed_file(v.get("filename") or "", changed_lines))
                and v.get("line") in changed_lines.get(matched_path, set())
            )
        ]
        if not filtered:
            return None
        return {**findings, "top_vulnerabilities": filtered}

    def analyze(
        self,
        pr_info: dict,
        diff: str,
        sonar_findings: Optional[dict] = None,
        checkmarx_findings: Optional[dict] = None,
    ) -> ReviewResult:
        """Analisa o PR e devolve ReviewResult."""

        diff_truncated = diff[:MAX_DIFF_CHARS]
        if len(diff) > MAX_DIFF_CHARS:
            diff_truncated += "\n\n[DIFF TRUNCADO — ficheiros restantes omitidos]"

        changed_lines = self._changed_lines(diff)
        sonar_findings = self._filter_sonar(sonar_findings, changed_lines)
        checkmarx_findings = self._filter_checkmarx(checkmarx_findings, changed_lines)

        user_msg = self._build_prompt(pr_info, diff_truncated, sonar_findings, checkmarx_findings)

        raw_text = self._call_copilot(user_msg).strip()
        # Extrai JSON de dentro de um bloco markdown, se presente
        md_match = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", raw_text)
        if md_match:
            raw_text = md_match.group(1)

        try:
            data = json.loads(raw_text)
        except json.JSONDecodeError as e:
            log.error(f"Resposta do modelo não é JSON válido: {e}\n{raw_text[:500]}")
            # Fallback seguro
            data = {
                "summary": "Erro ao analisar resposta da IA.",
                "verdict": "COMMENT",
                "severity": "minor",
                "issues": [],
                "general_comments": [raw_text[:1000]],
            }

        return ReviewResult(
            summary=data.get("summary", ""),
            verdict=data.get("verdict", "COMMENT"),
            severity=data.get("severity", "minor"),
            issues=data.get("issues", []),
            general_comments=data.get("general_comments", []),
            raw_json=data,
        )

    def _build_prompt(
        self,
        pr_info: dict,
        diff: str,
        sonar_findings: Optional[dict],
        checkmarx_findings: Optional[dict],
    ) -> str:
        lang_note = "em Português de Portugal" if self.language == "pt" else "in English"

        sections = [
            f"# Pull Request: {pr_info.get('title', 'N/A')}",
            f"**Autor:** {pr_info.get('user', {}).get('login', 'N/A')}",
            f"**Branch:** `{pr_info.get('head', {}).get('ref', 'N/A')}` → `{pr_info.get('base', {}).get('ref', 'N/A')}`",
            f"**Descrição:** {pr_info.get('body') or '(sem descrição)'}",
            "",
            f"Escreve todos os comentários {lang_note}.",
            "",
            "## Diff",
            "```diff",
            diff,
            "```",
        ]

        if sonar_findings:
            sections += [
                "",
                "## Resultados SonarQube",
                f"```json\n{json.dumps(sonar_findings, indent=2)}\n```",
            ]

        if checkmarx_findings:
            sections += [
                "",
                "## Resultados Checkmarx",
                f"```json\n{json.dumps(checkmarx_findings, indent=2)}\n```",
            ]

        sections += [
            "",
            "Analisa o código acima e devolve APENAS o JSON de review.",
        ]

        return "\n".join(sections)
