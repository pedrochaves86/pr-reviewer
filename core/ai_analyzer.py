"""
Analisador de código via GitHub Models.
Recebe diff + metadados e devolve uma review estruturada.
"""

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Optional
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

class AIAnalyzer:
    def __init__(self, settings: Settings):
        self._token = settings.github_models_token or settings.github_token
        self._endpoint = settings.github_models_endpoint
        self._catalog_endpoint = settings.github_models_catalog_endpoint
        self.model = settings.github_models_model
        self._validated_model: str | None = None
        self.language = settings.review_language

    def _resolve_validated_model(self) -> str:
        if self._validated_model:
            return self._validated_model

        target_model = self.model
        try:
            resp = requests.get(
                self._catalog_endpoint,
                headers={"Authorization": f"Bearer {self._token}"},
                timeout=20,
            )
            if resp.status_code in (401, 403):
                log.warning("Sem permissões para validar catálogo de modelos; a usar modelo configurado sem validação.")
                self._validated_model = target_model
                return self._validated_model

            resp.raise_for_status()
            payload = resp.json()
            raw_models = payload.get("data") if isinstance(payload, dict) else payload
            model_ids = []
            if isinstance(raw_models, list):
                for item in raw_models:
                    if isinstance(item, dict) and item.get("id"):
                        model_ids.append(str(item["id"]))
                    elif isinstance(item, str):
                        model_ids.append(item)

            if target_model in model_ids:
                self._validated_model = target_model
                return self._validated_model

            normalized_target = target_model.strip().lower()
            suffix_matches = [
                model_id for model_id in model_ids
                if f"/models/{normalized_target}/" in model_id.lower()
            ]
            if suffix_matches:
                # Mantém a versão mais recente quando o catálogo devolve múltiplas versões.
                self._validated_model = max(suffix_matches)
                log.info(
                    f"Modelo '{target_model}' validado no catálogo como '{self._validated_model}'."
                )
                return self._validated_model

            log.warning(
                f"Modelo '{target_model}' não encontrado no catálogo acessível. A usar valor configurado diretamente."
            )
        except requests.RequestException as exc:
            log.warning(f"Falha ao validar catálogo de modelos: {exc}")

        self._validated_model = target_model
        return self._validated_model

    def _call_github_models(self, user_msg: str) -> str:
        if not self._token:
            raise RuntimeError(
                "GITHUB_MODELS_TOKEN (ou GITHUB_TOKEN) não definido para chamar GitHub Models."
            )

        model = self._resolve_validated_model()

        payload = {
            "model": model,
            "max_tokens": 4096,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_msg},
            ],
        }

        log.info(f"A usar GitHub Models (modelo: {model})...")
        resp = requests.post(
            self._endpoint,
            headers={
                "Authorization": f"Bearer {self._token}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=120,
        )

        if resp.status_code in (401, 403):
            raise self._build_models_auth_error(resp)
        if resp.status_code == 429:
            raise RuntimeError("GitHub Models rate limit atingido. Tenta novamente dentro de alguns minutos.")
        if resp.status_code == 400:
            details = ""
            try:
                details = resp.json().get("error", {}).get("message", "")
            except Exception:
                details = ""
            raise RuntimeError(f"Pedido inválido para GitHub Models: {details or 'HTTP 400'}")

        resp.raise_for_status()
        data = resp.json()
        message_content = (((data.get("choices") or [{}])[0].get("message") or {}).get("content"))

        if isinstance(message_content, str):
            return message_content

        if isinstance(message_content, list):
            chunks = [item.get("text", "") for item in message_content if isinstance(item, dict)]
            return "\n".join(part for part in chunks if part).strip()

        raise RuntimeError("Resposta inesperada do GitHub Models: campo choices[0].message.content ausente.")

    @staticmethod
    def _build_models_auth_error(resp: requests.Response) -> RuntimeError:
        details = ""
        try:
            err = resp.json().get("error", {})
            details = (err.get("details") or err.get("message") or "").strip()
        except Exception:
            details = ""

        lower_details = details.lower()
        if "models is disabled" in lower_details or "github models is disabled" in lower_details:
            return RuntimeError(
                "GitHub Models está desativado no tenant GitHub Enterprise. "
                "Pede ao administrador da EDP para ativar GitHub Models para a organização/enterprise."
            )

        if details:
            return RuntimeError(f"GitHub Models não autorizado (HTTP {resp.status_code}): {details}")

        return RuntimeError(
            "GitHub Models não autorizado para este token. "
            "Usa um token com acesso a model inference no GitHub Enterprise da EDP."
        )

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

        log.info(f"A enviar diff para GitHub Models ({len(diff_truncated)} chars)...")
        raw_text = self._call_github_models(user_msg).strip()
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
