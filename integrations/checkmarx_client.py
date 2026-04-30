"""
Integração Checkmarx One — autenticação via API Key.

Como obter a API Key:
  1. Acede a https://eu-2.ast.checkmarx.net/
  2. Login com tenant edp-certmng + EDP SSO
  3. Vai a Settings -> API Keys -> Generate API Key
  4. Copia o valor para CHECKMARX_API_KEY no .env
"""

import logging
from datetime import datetime, timezone, timedelta
from typing import Optional
import requests

from config.settings import Settings

log = logging.getLogger(__name__)

IAM_TOKEN_URL = "https://eu-2.iam.checkmarx.net/auth/realms/{tenant}/protocol/openid-connect/token"


class CheckmarxClient:
    def __init__(self, settings: Settings):
        self.url = settings.checkmarx_url.rstrip("/")
        self.tenant = settings.checkmarx_tenant
        self.api_key = settings.checkmarx_api_key
        self.enabled = bool(self.url and self.tenant and self.api_key)
        self._token: Optional[str] = None
        self._token_expiry: Optional[datetime] = None

    # ── Auth ───────────────────────────────────────────────────────────────

    def _authenticate(self) -> bool:
        """Troca API Key por access token via Checkmarx One IAM."""
        now = datetime.now(timezone.utc)
        if self._token and self._token_expiry and now < self._token_expiry:
            return True

        try:
            url = IAM_TOKEN_URL.format(tenant=self.tenant)
            resp = requests.post(
                url,
                data={
                    "grant_type": "refresh_token",
                    "client_id": "ast-app",
                    "refresh_token": self.api_key,
                },
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()
            self._token = data.get("access_token")
            expires_in = int(data.get("expires_in", 300))
            self._token_expiry = now + timedelta(seconds=expires_in - 30)
            log.debug("Token Checkmarx One renovado.")
            return bool(self._token)
        except requests.RequestException as e:
            log.warning(f"Falha de autenticacao Checkmarx One: {e}")
            return False

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self._token}", "Content-Type": "application/json"}

    # ── Dados ──────────────────────────────────────────────────────────────

    def get_latest_scan_results(self, project_name: str) -> Optional[dict]:
        """
        Obtém vulnerabilidades do último scan SAST do projeto no Checkmarx One.
        Devolve dict com findings ou None se não disponível.
        """
        if not self.enabled:
            log.debug("Checkmarx One nao configurado — a ignorar.")
            return None

        if not self._authenticate():
            return None

        try:
            # 1. Encontrar o projeto pelo nome
            projects_resp = requests.get(
                f"{self.url}/api/projects",
                headers=self._headers(),
                params={"name": project_name, "limit": 10},
                timeout=15,
            )
            projects_resp.raise_for_status()
            projects_data = projects_resp.json()
            projects = projects_data.get("projects", [])

            project = next(
                (p for p in projects if project_name.lower() in p.get("name", "").lower()),
                None,
            )
            if not project:
                log.warning(f"Projeto Checkmarx '{project_name}' nao encontrado.")
                return None

            project_id = project["id"]

            # 2. Último scan SAST do projeto
            scans_resp = requests.get(
                f"{self.url}/api/scans",
                headers=self._headers(),
                params={
                    "project-id": project_id,
                    "scan-status": "Completed",
                    "limit": 1,
                    "sort": "-created_at",
                },
                timeout=15,
            )
            scans_resp.raise_for_status()
            scans = scans_resp.json().get("scans", [])
            if not scans:
                log.info(f"Sem scans concluidos para '{project_name}'.")
                return None

            scan_id = scans[0]["id"]

            # 3. Resumo de vulnerabilidades do scan
            summary_resp = requests.get(
                f"{self.url}/api/scan-summary",
                headers=self._headers(),
                params={"scan-ids": scan_id},
                timeout=15,
            )
            summary_resp.raise_for_status()
            summaries = summary_resp.json().get("scansSummaries", [])
            summary = summaries[0] if summaries else {}

            # 4. Top vulnerabilidades SAST (HIGH/CRITICAL)
            results_resp = requests.get(
                f"{self.url}/api/results",
                headers=self._headers(),
                params={
                    "scan-id": scan_id,
                    "severity": "HIGH,CRITICAL",
                    "limit": 20,
                },
                timeout=15,
            )
            results_resp.raise_for_status()
            results = results_resp.json().get("results", [])

            log.info(f"Checkmarx One: scan {scan_id} — {len(results)} vulnerabilidade(s) HIGH/CRITICAL.")
            return {
                "scan_id": scan_id,
                "project": project.get("name"),
                "summary": summary,
                "top_vulnerabilities": [
                    {
                        "type": r.get("type"),
                        "severity": r.get("severity"),
                        "state": r.get("state"),
                        "query_name": r.get("data", {}).get("queryName"),
                        "filename": r.get("data", {}).get("nodes", [{}])[0].get("fileName"),
                        "line": r.get("data", {}).get("nodes", [{}])[0].get("line"),
                        "description": r.get("description"),
                    }
                    for r in results[:15]
                ],
            }

        except requests.RequestException as e:
            log.warning(f"Erro ao consultar Checkmarx One: {e}")
            return None
