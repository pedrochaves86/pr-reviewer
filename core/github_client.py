"""
Cliente GitHub — obtém diff/ficheiros do PR e submete reviews.
"""

import re
import logging
from typing import Optional
import requests

from config.settings import Settings

log = logging.getLogger(__name__)

PR_URL_PATTERN = re.compile(
    r"https://github\.com/(?P<owner>[^/]+)/(?P<repo>[^/]+)/pull/(?P<number>\d+)"
)


class GitHubClient:
    API_BASE = "https://api.github.com"
    MANAGED_COMMENT_PREFIX = "<!-- pr-auto-reviewer:scan-result sha="

    def __init__(self, settings: Settings):
        self.settings = settings
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Authorization": f"Bearer {settings.github_token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            }
        )

    def parse_pr_url(self, url: str) -> tuple[str, str, int]:
        """Extrai (owner, repo, pr_number) de uma URL de PR."""
        m = PR_URL_PATTERN.match(url.strip())
        if not m:
            raise ValueError(f"URL de PR inválida: {url}")
        return m.group("owner"), m.group("repo"), int(m.group("number"))

    def get_pr_info(self, owner: str, repo: str, pr_number: int) -> dict:
        """Metadados do PR (título, autor, branch, descrição)."""
        url = f"{self.API_BASE}/repos/{owner}/{repo}/pulls/{pr_number}"
        resp = self.session.get(url, timeout=30)
        resp.raise_for_status()
        data = resp.json()

        # O GitHub pode devolver mergeable=None na primeira leitura enquanto calcula o estado.
        if data.get("mergeable") is None:
            retry = self.session.get(url, timeout=30)
            retry.raise_for_status()
            data = retry.json()

        return data

    @staticmethod
    def get_pr_merge_status(pr_info: dict) -> dict:
        """Resume o estado de merge do PR para detectar conflitos ou branch desactualizada."""
        mergeable = pr_info.get("mergeable")
        mergeable_state = (pr_info.get("mergeable_state") or "unknown").lower()

        has_conflicts = mergeable is False or mergeable_state == "dirty"
        is_behind = mergeable_state == "behind"

        return {
            "mergeable": mergeable,
            "mergeable_state": mergeable_state,
            "has_conflicts": has_conflicts,
            "is_behind": is_behind,
            "needs_attention": has_conflicts or is_behind,
        }

    def get_pr_files(self, owner: str, repo: str, pr_number: int) -> list[dict]:
        """Lista de ficheiros alterados com patch/diff."""
        url = f"{self.API_BASE}/repos/{owner}/{repo}/pulls/{pr_number}/files"
        files = []
        page = 1
        while True:
            resp = self.session.get(url, params={"per_page": 100, "page": page}, timeout=30)
            resp.raise_for_status()
            batch = resp.json()
            if not batch:
                break
            files.extend(batch)
            if len(batch) < 100:
                break
            page += 1
        log.info(f"PR #{pr_number}: {len(files)} ficheiro(s) alterado(s).")
        return files

    def get_pr_diff(self, owner: str, repo: str, pr_number: int) -> str:
        """Diff unificado completo do PR (formato patch)."""
        url = f"{self.API_BASE}/repos/{owner}/{repo}/pulls/{pr_number}"
        resp = self.session.get(
            url,
            headers={**self.session.headers, "Accept": "application/vnd.github.diff"},
            timeout=60,
        )
        resp.raise_for_status()
        return resp.text

    def get_latest_commit_sha(self, owner: str, repo: str, pr_number: int) -> str:
        """SHA do último commit do PR (necessário para submeter review)."""
        pr = self.get_pr_info(owner, repo, pr_number)
        return pr["head"]["sha"]

    def list_issue_comments(self, owner: str, repo: str, pr_number: int) -> list[dict]:
        """Lista comentários gerais do PR."""
        url = f"{self.API_BASE}/repos/{owner}/{repo}/issues/{pr_number}/comments"
        comments = []
        page = 1
        while True:
            resp = self.session.get(url, params={"per_page": 100, "page": page}, timeout=30)
            resp.raise_for_status()
            batch = resp.json()
            if not batch:
                break
            comments.extend(batch)
            if len(batch) < 100:
                break
            page += 1
        return comments

    def list_pr_reviews(self, owner: str, repo: str, pr_number: int) -> list[dict]:
        """Lista reviews submetidas no PR."""
        url = f"{self.API_BASE}/repos/{owner}/{repo}/pulls/{pr_number}/reviews"
        reviews = []
        page = 1
        while True:
            resp = self.session.get(url, params={"per_page": 100, "page": page}, timeout=30)
            resp.raise_for_status()
            batch = resp.json()
            if not batch:
                break
            reviews.extend(batch)
            if len(batch) < 100:
                break
            page += 1
        return reviews

    def _graphql(self, query: str, variables: dict) -> dict:
        resp = self.session.post(
            f"{self.API_BASE}/graphql",
            json={"query": query, "variables": variables},
            timeout=30,
        )
        resp.raise_for_status()
        payload = resp.json()
        if payload.get("errors"):
            raise requests.HTTPError(str(payload["errors"]))
        return payload.get("data", {})

    def _managed_comment_marker(self, head_sha: str) -> str:
        return f"{self.MANAGED_COMMENT_PREFIX}{head_sha} -->"

    def _is_managed_comment(self, comment: dict) -> bool:
        author = ((comment.get("user") or {}).get("login") or "").lower()
        reviewer = (self.settings.github_reviewer_login or "").lower()
        body = comment.get("body") or ""
        return author == reviewer and self.MANAGED_COMMENT_PREFIX in body

    def _is_bot_review(self, review: dict) -> bool:
        author = ((review.get("user") or {}).get("login") or "").lower()
        reviewer = (self.settings.github_reviewer_login or "").lower()
        return author == reviewer

    def _build_managed_comment_body(self, body: str, head_sha: str) -> str:
        return f"{body}\n\n{self._managed_comment_marker(head_sha)}"

    def _delete_issue_comment(self, owner: str, repo: str, comment_id: int):
        url = f"{self.API_BASE}/repos/{owner}/{repo}/issues/comments/{comment_id}"
        resp = self.session.delete(url, timeout=30)
        resp.raise_for_status()

    def _minimize_comment(self, node_id: str, classifier: str = "OUTDATED"):
        mutation = """
        mutation MinimizeComment($subjectId: ID!, $classifier: ReportedContentClassifiers!) {
          minimizeComment(input: {subjectId: $subjectId, classifier: $classifier}) {
            minimizedComment {
              isMinimized
              minimizedReason
            }
          }
        }
        """
        try:
            self._graphql(mutation, {"subjectId": node_id, "classifier": classifier})
        except requests.RequestException as e:
            log.warning(f"Não foi possível minimizar comentário/review antiga: {e}")

    def minimize_outdated_scan_interactions(
        self,
        owner: str,
        repo: str,
        pr_number: int,
        current_comment_id: Optional[int] = None,
    ):
        """Minimiza interações antigas do bot no PR, mantendo apenas o resultado atual visível."""
        for comment in self.list_issue_comments(owner, repo, pr_number):
            if not self._is_managed_comment(comment):
                continue
            if current_comment_id and comment.get("id") == current_comment_id:
                continue
            node_id = comment.get("node_id")
            if node_id:
                self._minimize_comment(node_id)

        for review in self.list_pr_reviews(owner, repo, pr_number):
            if not self._is_bot_review(review):
                continue
            node_id = review.get("node_id")
            if node_id:
                self._minimize_comment(node_id)

    def publish_scan_result(self, owner: str, repo: str, pr_number: int, body: str, head_sha: str) -> dict:
        """Mantém apenas um comentário gerido do bot por SHA do PR."""
        comments = self.list_issue_comments(owner, repo, pr_number)
        managed = [c for c in comments if self._is_managed_comment(c)]
        marker = self._managed_comment_marker(head_sha)
        same_sha = [c for c in managed if marker in (c.get("body") or "")]

        payload = {"body": self._build_managed_comment_body(body, head_sha)}

        if same_sha:
            current = max(same_sha, key=lambda c: c.get("updated_at") or c.get("created_at") or "")
            url = f"{self.API_BASE}/repos/{owner}/{repo}/issues/comments/{current['id']}"
            resp = self.session.patch(url, json=payload, timeout=30)
            resp.raise_for_status()

            for extra in same_sha:
                if extra["id"] != current["id"]:
                    self._delete_issue_comment(owner, repo, extra["id"])

            self.minimize_outdated_scan_interactions(
                owner=owner,
                repo=repo,
                pr_number=pr_number,
                current_comment_id=current["id"],
            )

            log.info(f"Comentário do scan actualizado → {resp.json().get('html_url')}")
            return resp.json()

        url = f"{self.API_BASE}/repos/{owner}/{repo}/issues/{pr_number}/comments"
        resp = self.session.post(url, json=payload, timeout=30)
        resp.raise_for_status()
        self.minimize_outdated_scan_interactions(
            owner=owner,
            repo=repo,
            pr_number=pr_number,
            current_comment_id=resp.json().get("id"),
        )
        log.info(f"Comentário do scan criado → {resp.json().get('html_url')}")
        return resp.json()

    def submit_review(
        self,
        owner: str,
        repo: str,
        pr_number: int,
        body: str,
        event: str,  # "COMMENT" | "REQUEST_CHANGES" | "APPROVE"
        comments: Optional[list[dict]] = None,
    ) -> dict:
        """
        Submete uma review ao PR.

        event:
          - "COMMENT"          → comentário geral sem aprovar/reprovar
          - "REQUEST_CHANGES"  → pede alterações (bloqueia merge)
          - "APPROVE"          → aprova

        comments: lista de inline comments, cada um com:
          { path, position, body }
        """
        commit_id = self.get_latest_commit_sha(owner, repo, pr_number)
        url = f"{self.API_BASE}/repos/{owner}/{repo}/pulls/{pr_number}/reviews"

        payload: dict = {
            "commit_id": commit_id,
            "body": body,
            "event": event,
        }
        if comments:
            payload["comments"] = comments

        resp = self.session.post(url, json=payload, timeout=30)
        if resp.status_code == 422:
            log.warning(
                "422 ao submeter review (provavelmente já existe review tua). "
                "A tentar como comentário geral..."
            )
            payload.pop("comments", None)
            payload["event"] = "COMMENT"
            resp = self.session.post(url, json=payload, timeout=30)
        resp.raise_for_status()
        log.info(f"Review submetida → {resp.json().get('html_url')}")
        return resp.json()

    def add_pr_comment(self, owner: str, repo: str, pr_number: int, body: str) -> dict:
        """Adiciona um comentário simples na thread do PR."""
        url = f"{self.API_BASE}/repos/{owner}/{repo}/issues/{pr_number}/comments"
        resp = self.session.post(url, json={"body": body}, timeout=30)
        resp.raise_for_status()
        return resp.json()
