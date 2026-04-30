"""
Formata o ReviewResult em texto Markdown para a review GitHub
e constrói a lista de inline comments.
"""

from core.ai_analyzer import ReviewResult

SEVERITY_EMOJI = {
    "critical": "🔴",
    "major": "🟠",
    "minor": "🟡",
    "clean": "🟢",
}

CATEGORY_LABEL = {
    "bug": "🐛 Bug",
    "security": "🔐 Segurança",
    "performance": "⚡ Performance",
    "maintainability": "🔧 Manutenibilidade",
    "style": "✨ Estilo",
    "sonar": "📊 SonarQube",
    "checkmarx": "🛡️ Checkmarx",
}

VERDICT_LABEL = {
    "APPROVE": "✅ Aprovado",
    "REQUEST_CHANGES": "🚫 Requer Alterações",
    "COMMENT": "💬 Comentário",
}


def format_review_body(result: ReviewResult, pr_title: str) -> str:
    """Gera o corpo principal da review em Markdown."""
    emoji = SEVERITY_EMOJI.get(result.severity, "⚪")
    verdict_label = VERDICT_LABEL.get(result.verdict, result.verdict)

    lines = [
        f"## {emoji} Code Review — {pr_title}",
        "",
        f"**Veredicto:** {verdict_label}  ",
        f"**Severidade:** `{result.severity}`",
        "",
        "### 📋 Resumo",
        result.summary,
        "",
    ]

    # Problemas por severidade
    critical = [i for i in result.issues if i.get("severity") == "critical"]
    major = [i for i in result.issues if i.get("severity") == "major"]
    minor = [i for i in result.issues if i.get("severity") == "minor"]

    if result.issues:
        lines += ["### 🔍 Problemas Encontrados", ""]
        for group, label in [(critical, "Críticos 🔴"), (major, "Major 🟠"), (minor, "Minor 🟡")]:
            if group:
                lines += [f"#### {label}", ""]
                for issue in group:
                    cat = CATEGORY_LABEL.get(issue.get("category", ""), issue.get("category", ""))
                    file_ref = issue.get("file", "")
                    line_ref = f":{issue['line_hint']}" if issue.get("line_hint") else ""
                    lines += [
                        f"**{cat}** — `{file_ref}{line_ref}`",
                        "",
                        f"> {issue.get('message', '')}",
                        "",
                    ]
                    if issue.get("suggestion"):
                        lines += [
                            "<details><summary>💡 Sugestão</summary>",
                            "",
                            f"```\n{issue['suggestion']}\n```",
                            "",
                            "</details>",
                            "",
                        ]
    else:
        lines += ["### ✅ Sem problemas críticos encontrados", ""]

    # Comentários gerais
    if result.general_comments:
        lines += ["### 💡 Comentários Gerais", ""]
        for comment in result.general_comments:
            lines += [f"- {comment}"]
        lines.append("")

    lines += [
        "---",
        "*Review gerada automaticamente pelo PR Auto-Reviewer bot. "
        "Valida os pontos levantados antes de fazer merge.*",
    ]

    return "\n".join(lines)


def build_inline_comments(result: ReviewResult, pr_files: list[dict]) -> list[dict]:
    """
    Tenta construir inline comments para issues com ficheiro conhecido.
    O GitHub exige `position` (linha no diff), não nº de linha absoluto.
    Mapeia de forma best-effort.
    """
    # Mapa: filename → lista de posições de diff
    diff_positions: dict[str, list[int]] = {}
    for f in pr_files:
        patch = f.get("patch", "")
        diff_positions[f["filename"]] = list(range(1, patch.count("\n") + 2))

    inline = []
    for issue in result.issues:
        filename = issue.get("file", "")
        if not filename or filename not in diff_positions:
            continue

        positions = diff_positions[filename]
        if not positions:
            continue

        # Tenta usar a linha sugerida; se não disponível, aponta para posição 1
        try:
            line_hint = int(issue.get("line_hint") or 1)
            position = min(line_hint, max(positions))
        except (ValueError, TypeError):
            position = 1

        cat = CATEGORY_LABEL.get(issue.get("category", ""), issue.get("category", ""))
        body = f"**{cat}** {SEVERITY_EMOJI.get(issue.get('severity', 'minor'), '')}\n\n"
        body += issue.get("message", "")
        if issue.get("suggestion"):
            body += f"\n\n**Sugestão:**\n```\n{issue['suggestion']}\n```"

        inline.append({"path": filename, "position": position, "body": body})

    return inline
