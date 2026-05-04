#!/usr/bin/env python3
"""
PR Auto-Reviewer Bot
Processa PRs GitHub por URL e submete reviews automáticas.
"""

import logging
import sys
from config.settings import Settings
from core.pr_processor import PRProcessor

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    handlers=[
        logging.StreamHandler(stream=open(sys.stdout.fileno(), mode='w', encoding='utf-8', closefd=False)),
        logging.FileHandler("pr_reviewer.log", encoding="utf-8"),
    ],
)
log = logging.getLogger("main")


def process_direct_prs(settings: Settings, processor: PRProcessor):
    """Processa um ou vários URLs de PR definidos em GITHUB_PR_URL."""
    raw = settings.github_pr_url.strip()
    pr_urls = [u.strip() for u in raw.split(",") if u.strip()]

    if not pr_urls:
        raise ValueError("GITHUB_PR_URL foi definido, mas não contém URL(s) válida(s).")

    log.info(f"Modo direto ativo: {len(pr_urls)} PR(s) para processar.")
    for pr_url in pr_urls:
        log.info(f"A processar PR direto: {pr_url}")
        processor.process(pr_url)
    log.info("Processamento direto concluído.")


def main():
    log.info("PR Auto-Reviewer a iniciar...")
    settings = Settings()
    settings.validate()

    processor = PRProcessor(settings)
    process_direct_prs(settings, processor)


if __name__ == "__main__":
    main()
