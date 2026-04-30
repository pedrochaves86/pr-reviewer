"""
Integração SonarQube — tenta obter issues via API REST.
Se não configurado ou falhar, devolve None (análise estática pela IA).
"""

import logging
from typing import Optional
import requests

from config.settings import Settings

log = logging.getLogger(__name__)


class SonarClient:
    def __init__(self, settings: Settings):
        self.url = settings.sonar_url.rstrip("/")
        self.token = settings.sonar_token
        self.enabled = bool(self.url and self.token)

    def get_issues(self, project_key: str, branch: Optional[str] = None) -> Optional[dict]:
        """
        Obtém issues do projeto no SonarQube.
        Devolve dict com issues ou None se não disponível.
        """
        if not self.enabled:
            log.debug("SonarQube não configurado — a ignorar.")
            return None

        try:
            params = {
                "componentKeys": project_key,
                "resolved": "false",
                "ps": 100,
                "types": "BUG,VULNERABILITY,CODE_SMELL",
            }
            if branch:
                params["branch"] = branch

            resp = requests.get(
                f"{self.url}/api/issues/search",
                params=params,
                auth=(self.token, ""),
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()

            issues = data.get("issues", [])
            log.info(f"SonarQube: {len(issues)} issue(s) encontrado(s) em '{project_key}'.")

            return {
                "total": data.get("total", 0),
                "issues": [
                    {
                        "key": i.get("key"),
                        "type": i.get("type"),
                        "severity": i.get("severity"),
                        "message": i.get("message"),
                        "component": i.get("component"),
                        "line": i.get("line"),
                        "rule": i.get("rule"),
                    }
                    for i in issues[:50]  # limita contexto
                ],
            }

        except requests.RequestException as e:
            log.warning(f"Não foi possível contactar SonarQube: {e}")
            return None

    def derive_project_key(self, owner: str, repo: str) -> str:
        """Tenta inferir a project key — por convenção owner:repo ou só repo."""
        return f"{owner}_{repo}"
