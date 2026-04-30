"""
Orquestrador principal — liga todas as peças:
Teams → GitHub → Sonar/Checkmarx → Claude → Review
"""

import logging
from config.settings import Settings
from core.github_client import GitHubClient
from core.ai_analyzer import AIAnalyzer
from core.review_formatter import format_review_body
from core.teams_monitor import TeamsMonitor
from integrations.sonar_client import SonarClient
from integrations.checkmarx_client import CheckmarxClient

log = logging.getLogger(__name__)


class PRProcessor:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.github = GitHubClient(settings)
        self.analyzer = AIAnalyzer(settings)
        self.sonar = SonarClient(settings)
        self.checkmarx = CheckmarxClient(settings)
        # TeamsMonitor partilhado para marcar PRs como processados
        self._monitor: TeamsMonitor | None = None

    def set_monitor(self, monitor: TeamsMonitor):
        self._monitor = monitor

    def process(self, pr_url: str):
        """Pipeline completo para um URL de PR."""
        log.info(f"━━━ A processar PR: {pr_url} ━━━")

        # 1. Parse URL
        owner, repo, pr_number = self.github.parse_pr_url(pr_url)

        # 2. Metadados do PR
        log.info("📄 A obter metadados do PR...")
        pr_info = self.github.get_pr_info(owner, repo, pr_number)
        merge_status = self.github.get_pr_merge_status(pr_info)
        head_sha = pr_info.get("head", {}).get("sha", "")
        branch = pr_info.get("head", {}).get("ref", "")
        pr_title = pr_info.get("title", f"PR #{pr_number}")
        pr_author = pr_info.get("user", {}).get("login", "")

        log.info(f"   Título: {pr_title}")
        log.info(f"   Autor:  {pr_author} | Branch: {branch}")
        if merge_status["has_conflicts"]:
            log.warning("   Estado GitHub: PR com conflitos de merge.")
        elif merge_status["is_behind"]:
            log.warning("   Estado GitHub: branch do PR não está up-to-date com a base.")

        # 3. Diff
        log.info("📦 A obter diff...")
        diff = self.github.get_pr_diff(owner, repo, pr_number)
        self.github.get_pr_files(owner, repo, pr_number)

        # 4. SonarQube (best-effort)
        sonar_findings = None
        if self.sonar.enabled:
            log.info("📊 A consultar SonarQube...")
            project_key = self.sonar.derive_project_key(owner, repo)
            sonar_findings = self.sonar.get_issues(project_key, branch)

        # 5. Checkmarx (best-effort)
        cx_findings = None
        if self.checkmarx.enabled:
            log.info("🛡️  A consultar Checkmarx...")
            cx_findings = self.checkmarx.get_latest_scan_results(repo)

        # 6. Análise Claude
        log.info("🤖 A analisar código com Claude...")
        result = self.analyzer.analyze(
            pr_info=pr_info,
            diff=diff,
            sonar_findings=sonar_findings,
            checkmarx_findings=cx_findings,
        )

        if merge_status["has_conflicts"]:
            result.general_comments.insert(
                0,
                "O GitHub indica conflitos de merge neste PR. Resolve os conflitos com a branch base antes de fazer merge.",
            )
            result.verdict = "REQUEST_CHANGES"
            if result.severity == "clean":
                result.severity = "major"
            result.summary = (
                "O PR tem conflitos de merge com a branch base. "
                f"{result.summary}"
            ).strip()
        elif merge_status["is_behind"]:
            result.general_comments.insert(
                0,
                "O GitHub indica que a branch do PR está desactualizada face à branch base. Convém actualizar e validar novamente antes do merge.",
            )
            if result.verdict == "APPROVE":
                result.verdict = "COMMENT"
            result.summary = (
                "A branch do PR não está up-to-date com a branch base. "
                f"{result.summary}"
            ).strip()

        log.info(
            f"   Veredicto: {result.verdict} | Severidade: {result.severity} | "
            f"Issues: {len(result.issues)}"
        )

        # 7. Formata review
        review_body = format_review_body(result, pr_title)

        # 8. Publica resultado do scan no GitHub
        log.info(f"📝 A publicar resultado do scan ({result.verdict})...")
        self.github.publish_scan_result(
            owner=owner,
            repo=repo,
            pr_number=pr_number,
            body=review_body,
            head_sha=head_sha,
        )

        # 9. Marca como processado
        if self._monitor:
            self._monitor.mark_processed(pr_url)

        log.info(f"✅ PR #{pr_number} processado com sucesso.")
        return result
