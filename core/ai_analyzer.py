"""
Analisador de código via Claude (Anthropic API).
Recebe diff + metadados e devolve uma review estruturada.
"""

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Optional
import anthropic
from anthropic import APIStatusError, APIConnectionError, AuthenticationError, RateLimitError
import requests

from config.settings import Settings

log = logging.getLogger(__name__)

MAX_DIFF_CHARS = 20_000  # ~5k tokens — mantém dentro do limite de 10k tokens/min do plano free


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


# Erros que afetam toda a conta — não adianta tentar outro modelo
_ACCOUNT_LEVEL_ERRORS = ("credit", "balance", "billing", "unauthorized", "invalid x-api-key")


def _is_account_level_error(exc: APIStatusError) -> bool:
    """True quando o erro afeta toda a conta (créditos, autenticação) — fallback inútil."""
    msg = ""
    try:
        msg = exc.body.get("error", {}).get("message", "") if isinstance(exc.body, dict) else str(exc.body)
    except Exception:
        pass
    return any(keyword in msg.lower() for keyword in _ACCOUNT_LEVEL_ERRORS)


OPENAI_API = "https://api.openai.com/v1/chat/completions"


class _OpenAIResponse:
    """Wrapper mínimo para normalizar a resposta da OpenAI API ao formato Anthropic."""
    def __init__(self, text: str):
        self.content = [type("_Block", (), {"text": text})()]
        self.usage = type("_Usage", (), {"cache_read_input_tokens": 0})()


class AIAnalyzer:
    def __init__(self, settings: Settings):
        self.client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
        self.model = settings.claude_model
        self._fallback_models = [
            m for m in settings.claude_fallback_models if m != self.model
        ]
        self.language = settings.review_language
        self._openai_api_key = settings.openai_api_key
        self._openai_model = settings.openai_model

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

    def _call_with_fallback(self, user_msg: str):
        """Tenta Anthropic (modelo principal + fallbacks) e, se a conta falhar, usa OpenAI."""
        models_to_try = [self.model] + self._fallback_models
        last_exc: Exception | None = None

        for attempt, model in enumerate(models_to_try):
            if attempt > 0:
                log.warning(f"A tentar modelo de fallback Anthropic: {model}")
            try:
                response = self.client.messages.create(
                    model=model,
                    max_tokens=4096,
                    system=[{"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}],
                    messages=[{"role": "user", "content": user_msg}],
                )
                if attempt > 0:
                    log.info(f"Sucesso com modelo de fallback Anthropic: {model}")
                return response
            except (AuthenticationError, RateLimitError, APIConnectionError):
                raise
            except APIStatusError as e:
                if _is_account_level_error(e):
                    log.warning(f"Conta Anthropic indisponível ({e.status_code}). A tentar OpenAI...")
                    return self._call_openai(user_msg)
                log.warning(f"Modelo '{model}' falhou ({e.status_code}): {e.message}")
                last_exc = e

        raise last_exc  # type: ignore[misc]

    def _call_openai(self, user_msg: str):
        """Chama a OpenAI API como fallback quando a conta Anthropic não está disponível."""
        if not self._openai_api_key:
            raise RuntimeError(
                "Anthropic sem créditos e OPENAI_API_KEY não configurada. "
                "Adiciona créditos em https://console.anthropic.com/settings/billing "
                "ou define OPENAI_API_KEY no .env (https://platform.openai.com/api-keys)."
            )

        log.info(f"A usar OpenAI como fallback (modelo: {self._openai_model})...")
        resp = requests.post(
            OPENAI_API,
            headers={
                "Authorization": f"Bearer {self._openai_api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": self._openai_model,
                "max_tokens": 4096,
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": user_msg},
                ],
            },
            timeout=120,
        )
        if resp.status_code == 401:
            raise RuntimeError("OpenAI API key inválida ou sem permissões. Verifica OPENAI_API_KEY.")
        if resp.status_code == 429:
            err_code = resp.json().get("error", {}).get("code", "")
            if err_code == "insufficient_quota":
                raise RuntimeError(
                    "Quota OpenAI esgotada — a conta não tem créditos. "
                    "Adiciona créditos em https://platform.openai.com/settings/billing/overview "
                    "ou recarrega a conta Anthropic em https://console.anthropic.com/settings/billing."
                )
            raise RuntimeError("OpenAI rate limit atingido. Tenta novamente dentro de alguns segundos.")
        resp.raise_for_status()
        text = resp.json()["choices"][0]["message"]["content"]
        log.info(f"Resposta recebida de OpenAI ({self._openai_model}).")
        return _OpenAIResponse(text)

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

        log.info(f"A enviar diff para Claude ({len(diff_truncated)} chars)...")
        response = self._call_with_fallback(user_msg)

        cache_read = getattr(response.usage, "cache_read_input_tokens", 0)
        if cache_read:
            log.debug(f"Prompt cache hit: {cache_read} tokens lidos da cache.")

        raw_text = response.content[0].text.strip()
        # Extrai JSON de dentro de um bloco markdown, se presente
        md_match = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", raw_text)
        if md_match:
            raw_text = md_match.group(1)

        try:
            data = json.loads(raw_text)
        except json.JSONDecodeError as e:
            log.error(f"Resposta Claude não é JSON válido: {e}\n{raw_text[:500]}")
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
