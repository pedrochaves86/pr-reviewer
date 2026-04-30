"""
Monitorização do canal Teams via Microsoft Graph API.
Deteta mensagens com links de PR do GitHub.
"""

import re
import json
import logging
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.parse import urlparse
from typing import Optional
import requests

from config.settings import Settings

log = logging.getLogger(__name__)

# Regex para apanhar URLs de PR do GitHub
PR_URL_PATTERN = re.compile(
    r"https://github\.com/([^/\s]+)/([^/\s]+)/pull/(\d+)"
)


class TeamsMonitor:
    GRAPH_BASE = "https://graph.microsoft.com/v1.0"
    TOKEN_URL = "https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token"

    def __init__(self, settings: Settings):
        self.settings = settings
        self._token: Optional[str] = None
        self._token_expiry: Optional[datetime] = None
        self._processed_file = Path(settings.processed_prs_file)
        self._processed: set[str] = self._load_processed()

    # ── Auth ───────────────────────────────────────────────────────────────

    def _get_token(self) -> str:
        """Obtém (ou renova) token OAuth2 para Microsoft Graph."""
        now = datetime.now(timezone.utc)
        if self._token and self._token_expiry and now < self._token_expiry:
            return self._token

        url = self.TOKEN_URL.format(tenant=self.settings.teams_tenant_id)
        resp = requests.post(
            url,
            data={
                "grant_type": "client_credentials",
                "client_id": self.settings.teams_client_id,
                "client_secret": self.settings.teams_client_secret,
                "scope": "https://graph.microsoft.com/.default",
            },
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        self._token = data["access_token"]
        expires_in = int(data.get("expires_in", 3600))
        self._token_expiry = now + timedelta(seconds=expires_in - 60)
        log.debug("Token Microsoft Graph renovado.")
        return self._token

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self._get_token()}", "Content-Type": "application/json"}

    def _is_chat_target(self) -> bool:
        """Deteta se TEAMS_CHANNEL_ID aponta para chat (thread.v2) em vez de canal de team."""
        target = (self.settings.teams_channel_id or "").strip()
        return "@thread.v2" in target or "/l/chat/" in target

    def _extract_chat_id(self, value: str) -> str:
        """Extrai chatId de um valor TEAMS_CHANNEL_ID (id direto ou URL do Teams)."""
        raw = value.strip()
        if raw.startswith("http://") or raw.startswith("https://"):
            parsed = urlparse(raw)
            parts = parsed.path.split("/l/chat/")
            if len(parts) == 2 and parts[1]:
                return parts[1].split("/")[0]
        return raw

    # ── Leitura de mensagens ───────────────────────────────────────────────

    def fetch_new_pr_links(self) -> list[str]:
        """Devolve lista de URLs de PR ainda não processados."""
        messages = self._fetch_recent_messages()
        new_links: list[str] = []

        for msg in messages:
            body = msg.get("body", {}).get("content", "")
            for match in PR_URL_PATTERN.finditer(body):
                pr_url = match.group(0)
                if pr_url not in self._processed:
                    new_links.append(pr_url)

        return new_links

    def _fetch_recent_messages(self) -> list[dict]:
        """Busca mensagens das últimas 24h do canal ou chat configurado."""
        since = (datetime.now(timezone.utc) - timedelta(hours=24)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        if self._is_chat_target():
            chat_id = self._extract_chat_id(self.settings.teams_channel_id)
            url = f"{self.GRAPH_BASE}/chats/{chat_id}/messages?$top=50"
        else:
            url = (
                f"{self.GRAPH_BASE}/teams/{self.settings.teams_team_id}"
                f"/channels/{self.settings.teams_channel_id}/messages"
                f"?$filter=lastModifiedDateTime gt {since}&$top=50"
            )

        resp = requests.get(url, headers=self._headers(), timeout=30)
        resp.raise_for_status()
        messages = resp.json().get("value", [])

        # Em chats nem todos os tenants aceitam o filtro server-side, então filtramos localmente.
        if self._is_chat_target():
            messages = [
                msg for msg in messages
                if (msg.get("lastModifiedDateTime") or "") > since
            ]

        log.info(f"Mensagens lidas do Teams: {len(messages)}")
        return messages

    # ── Controlo de duplicados ─────────────────────────────────────────────

    def mark_processed(self, pr_url: str):
        self._processed.add(pr_url)
        self._save_processed()

    def _load_processed(self) -> set[str]:
        if self._processed_file.exists():
            try:
                return set(json.loads(self._processed_file.read_text()))
            except Exception:
                pass
        return set()

    def _save_processed(self):
        self._processed_file.write_text(
            json.dumps(sorted(self._processed), indent=2)
        )
