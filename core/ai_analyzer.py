"""
Analisador de código via gh models CLI.
Recebe diff + metadados e devolve uma review estruturada.
"""

import json
import logging
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from typing import Optional

from config.settings import Settings

log = logging.getLogger(__name__)

MAX_DIFF_CHARS = 20_000  # ~5k tokens




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
        self.model = settings.github_models_model
        self.org = settings.github_models_org
        self._validated_model: str | None = None
        self.language = settings.review_language

    @staticmethod
    def _run_gh_command(args: list[str], *, input_text: str | None = None, timeout: int = 120) -> str:
        gh_path = shutil.which("gh")
        if not gh_path:
            raise RuntimeError(
                "GitHub CLI (`gh`) não encontrado no PATH. "
                "Instala o GitHub CLI e a extensão `github/gh-models`."
            )

        cmd = [gh_path, *args]
        try:
            completed = subprocess.run(
                cmd,
                input=input_text,
                text=True,
                capture_output=True,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError("O comando `gh models` excedeu o tempo limite.") from exc

        stdout = (completed.stdout or "").strip()
        stderr = (completed.stderr or "").strip()
        if completed.returncode == 0:
            return stdout

        combined = "\n".join(part for part in [stderr, stdout] if part).strip()
        lower_combined = combined.lower()
        if "unknown command \"models\"" in lower_combined or "gh models" in lower_combined and "extension" in lower_combined:
            raise RuntimeError(
                "A extensão `gh models` não está instalada. "
                "Corre `gh extension install github/gh-models` e autentica-te com `gh auth login`."
            )
        if "gh auth login" in lower_combined or "no github token found" in lower_combined:
            raise RuntimeError(
                "O `gh models` não está autenticado. "
                "Corre `gh auth login` antes de executar o reviewer."
            )
        if '"code":"no_access"' in lower_combined or "no access to model" in lower_combined:
            raise RuntimeError(
                "A conta autenticada não tem acesso de inferência ao modelo configurado em `gh models`. "
                "Confirma no tenant quais modelos podem ser usados com `gh models run` ou define `GITHUB_MODELS_MODEL` para um modelo autorizado."
            )
        if combined:
            raise RuntimeError(f"Falha ao executar `{' '.join(cmd)}`: {combined}")
        raise RuntimeError(f"Falha ao executar `{' '.join(cmd)}` (exit code {completed.returncode}).")

    @staticmethod
    def _match_model(target_model: str, model_ids: list[str]) -> str | None:
        if target_model in model_ids:
            return target_model

        normalized_target = target_model.strip().lower()
        if not normalized_target:
            return None

        for model_id in model_ids:
            normalized_model_id = model_id.lower()
            if normalized_model_id == normalized_target:
                return model_id
            if normalized_model_id.endswith(f"/{normalized_target}"):
                return model_id

        return None

    def _resolve_validated_model(self) -> str:
        if self._validated_model:
            return self._validated_model

        target_model = self.model
        try:
            output = self._run_gh_command(["models", "list"], timeout=30)
            model_ids = []
            for line in output.splitlines():
                stripped = line.strip()
                if not stripped or stripped.startswith("ID ") or stripped.startswith("Showing "):
                    continue

                model_id = stripped.split()[0]
                if "/" in model_id:
                    model_ids.append(model_id)

            matched_model = self._match_model(target_model, model_ids)
            if matched_model:
                self._validated_model = matched_model
                if matched_model != target_model:
                    log.info(
                        f"Modelo '{target_model}' validado no catálogo como '{self._validated_model}'."
                    )
                return self._validated_model

            if model_ids:
                log.info(
                    f"Modelos disponíveis via `gh models list`: {', '.join(model_ids[:5])}"
                )

            log.warning(
                f"Modelo '{target_model}' não encontrado em `gh models list`. A usar valor configurado diretamente."
            )
        except RuntimeError as exc:
            log.warning(f"Falha ao validar catálogo de modelos via gh: {exc}")

        self._validated_model = target_model
        return self._validated_model

    def _call_github_models(self, user_msg: str) -> str:
        model = self._resolve_validated_model()

        cmd = [
            "models",
            "run",
            model,
            "--system-prompt",
            SYSTEM_PROMPT,
            "--max-tokens",
            "4096",
        ]
        if self.org:
            cmd.extend(["--org", self.org])

        log.info(f"A usar gh models (modelo: {model})...")
        return self._run_gh_command(cmd, input_text=user_msg, timeout=180)

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

        log.info(f"A enviar diff para gh models ({len(diff_truncated)} chars)...")
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
