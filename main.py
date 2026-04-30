#!/usr/bin/env python3
"""
PR Auto-Reviewer Bot
Monitoriza canal Teams, deteta PRs GitHub e submete reviews automáticas.
"""

import schedule
import time
import logging
import sys
from config.settings import Settings
from core.teams_monitor import TeamsMonitor
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


def run_cycle(monitor: TeamsMonitor, processor: PRProcessor):
    """Um ciclo completo: buscar mensagens → processar PRs novos."""
    log.info("A verificar canal Teams...")
    try:
        new_prs = monitor.fetch_new_pr_links()
        if not new_prs:
            log.info("Nenhum PR novo encontrado.")
            return

        for pr_url in new_prs:
            log.info(f"PR detetado: {pr_url}")
            try:
                processor.process(pr_url)
                log.info(f"Review submetida para {pr_url}")
            except Exception as e:
                log.error(f"Erro ao processar {pr_url}: {e}", exc_info=True)
    except Exception as e:
        log.error(f"Erro no ciclo de monitorização: {e}", exc_info=True)


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

    # Modo alternativo: processa URL(s) de PR diretamente e termina.
    if settings.github_pr_url.strip():
        process_direct_prs(settings, processor)
        return

    monitor = TeamsMonitor(settings)
    processor.set_monitor(monitor)

    interval = settings.poll_interval_minutes
    log.info(f"Intervalo de polling: {interval} minuto(s)")

    # Corre imediatamente na primeira vez
    run_cycle(monitor, processor)

    schedule.every(interval).minutes.do(run_cycle, monitor=monitor, processor=processor)

    while True:
        schedule.run_pending()
        time.sleep(30)


if __name__ == "__main__":
    main()
