"""
Formata o ReviewResult em texto Markdown para a review GitHub.
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
                            issue["suggestion"],
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
