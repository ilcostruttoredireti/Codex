#!/usr/bin/env python3
"""
Gmail → HubSpot Contact Sync
════════════════════════════
Monitora la inbox Gmail in tempo (quasi) reale e sincronizza i mittenti
come contatti in HubSpot.

Uso:
    python main.py                    # avvio normale (loop continuo)
    python main.py --once             # singola scansione e uscita
    python main.py --backfill 200     # processa le ultime N email e uscita
    python main.py --dry-run          # simula senza scrivere su HubSpot

Variabili d'ambiente richieste (vedi .env.example):
    HUBSPOT_API_KEY
    GMAIL_CREDENTIALS_FILE  (default: credentials.json)
"""
import argparse
import logging
import sys
import time
from datetime import datetime

from rich.console import Console
from rich.table import Table
from rich import box

from config import POLL_INTERVAL_SECONDS, STATE_FILE
from gmail_monitor import GmailMonitor
from hubspot_sync import HubSpotSync, SyncResult, SyncStatus
from state_manager import StateManager

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("sync.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger(__name__)
console = Console()


# ── Status display ────────────────────────────────────────────────────────────

STATUS_STYLE = {
    SyncStatus.CREATO:     "bold green",
    SyncStatus.AGGIORNATO: "bold yellow",
    SyncStatus.IGNORATO:   "dim",
    SyncStatus.ERRORE:     "bold red",
}

STATUS_ICON = {
    SyncStatus.CREATO:     "✅",
    SyncStatus.AGGIORNATO: "🔄",
    SyncStatus.IGNORATO:   "⏭️",
    SyncStatus.ERRORE:     "❌",
}


def print_result(result: SyncResult, subject: str = ""):
    icon  = STATUS_ICON.get(result.status, "?")
    style = STATUS_STYLE.get(result.status, "white")
    cid   = result.contact_id or "—"
    note  = f"  [{result.reason}]" if result.reason else ""
    console.print(
        f"  {icon} [{style}]{result.status.value:<12}[/{style}]  "
        f"[cyan]{result.email}[/cyan]  "
        f"ID: [magenta]{cid}[/magenta]"
        f"[dim]{note}[/dim]"
    )


def print_summary(results: list[SyncResult]):
    if not results:
        return
    counts = {s: 0 for s in SyncStatus}
    for r in results:
        counts[r.status] += 1

    table = Table(box=box.SIMPLE, show_header=True, header_style="bold white")
    table.add_column("Stato", style="bold")
    table.add_column("N°", justify="right")
    for status, n in counts.items():
        if n > 0:
            style = STATUS_STYLE[status]
            table.add_row(f"[{style}]{status.value}[/{style}]", str(n))
    console.print(table)


# ── Core scan ────────────────────────────────────────────────────────────────

def run_scan(
    gmail: GmailMonitor,
    hubspot: HubSpotSync,
    state: StateManager,
    max_messages: int = 50,
    dry_run: bool = False,
) -> list[SyncResult]:
    """
    Scansiona la inbox Gmail e sincronizza i nuovi mittenti su HubSpot.
    """
    messages = gmail.fetch_inbox_messages(max_results=max_messages)
    if not messages:
        logger.debug("Nessun messaggio trovato nella inbox.")
        return []

    new_messages = [m for m in messages if not state.is_processed(m["id"])]
    if not new_messages:
        logger.debug("Nessun nuovo messaggio da processare (tutti già visti).")
        return []

    console.print(
        f"\n[bold white]📬 {len(new_messages)} nuovi messaggi[/bold white] "
        f"({datetime.now().strftime('%H:%M:%S')})"
    )

    results: list[SyncResult] = []

    for msg_meta in new_messages:
        msg_id = msg_meta["id"]

        # Estrai info mittente
        sender = gmail.get_sender_info(msg_id)
        if sender is None:
            state.mark_processed(msg_id)
            continue

        logger.debug("Processo: %s | %s | %s", sender.email, sender.full_name, sender.subject)

        if dry_run:
            console.print(
                f"  [dim][DRY-RUN] Saltato: {sender.email} — "
                f"{sender.full_name} ({sender.company})[/dim]"
            )
            state.mark_processed(msg_id)
            continue

        # Sincronizza su HubSpot
        result = hubspot.sync_sender(sender)
        results.append(result)
        print_result(result, subject=sender.subject)

        # Segna come processato solo dopo sync riuscito (o ignorato intenzionalmente)
        if result.status != SyncStatus.ERRORE:
            state.mark_processed(msg_id)

    return results


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Sincronizza contatti Gmail → HubSpot"
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Esegui una sola scansione e termina",
    )
    parser.add_argument(
        "--backfill",
        type=int,
        metavar="N",
        help="Processa le ultime N email e termina (default: 50)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Simula senza scrivere su HubSpot",
    )
    parser.add_argument(
        "--reset-state",
        action="store_true",
        help="Cancella lo stato salvato e riparte da zero",
    )
    args = parser.parse_args()

    console.rule("[bold cyan]Gmail → HubSpot Sync[/bold cyan]")

    # ── Inizializzazione ────────────────────────────────────────────────────
    state = StateManager(STATE_FILE)

    if args.reset_state:
        import os
        if os.path.exists(STATE_FILE):
            os.remove(STATE_FILE)
            console.print(f"[yellow]Stato resettato ({STATE_FILE} rimosso)[/yellow]")
        state = StateManager(STATE_FILE)

    gmail   = GmailMonitor()
    hubspot = HubSpotSync() if not args.dry_run else None

    # ── Modalità backfill ───────────────────────────────────────────────────
    if args.backfill is not None:
        console.print(f"[cyan]Modalità backfill: ultime {args.backfill} email[/cyan]")
        results = run_scan(
            gmail,
            hubspot or _DryRunHubSpot(),
            state,
            max_messages=min(args.backfill, 500),
            dry_run=args.dry_run,
        )
        print_summary(results)
        console.print("[bold green]Backfill completato.[/bold green]")
        return

    # ── Modalità singola scansione ──────────────────────────────────────────
    if args.once:
        results = run_scan(
            gmail,
            hubspot or _DryRunHubSpot(),
            state,
            dry_run=args.dry_run,
        )
        print_summary(results)
        return

    # ── Modalità loop continuo ──────────────────────────────────────────────
    console.print(
        f"[bold green]▶ Avvio monitoraggio continuo "
        f"(ogni {POLL_INTERVAL_SECONDS}s)[/bold green]"
        + (" [DRY-RUN]" if args.dry_run else "")
    )
    console.print("  Premi CTRL+C per fermare.\n")

    all_results: list[SyncResult] = []
    try:
        while True:
            scan_results = run_scan(
                gmail,
                hubspot or _DryRunHubSpot(),
                state,
                dry_run=args.dry_run,
            )
            all_results.extend(scan_results)

            logger.debug(
                "Prossima scansione tra %ds — totale processati: %d",
                POLL_INTERVAL_SECONDS,
                state.count(),
            )
            time.sleep(POLL_INTERVAL_SECONDS)

    except KeyboardInterrupt:
        console.print("\n[yellow]Monitoraggio interrotto dall'utente.[/yellow]")
        print_summary(all_results)


class _DryRunHubSpot:
    """Stub HubSpot per la modalità dry-run."""
    def sync_sender(self, sender):
        from hubspot_sync import SyncResult, SyncStatus
        return SyncResult(
            status=SyncStatus.IGNORATO,
            email=sender.email,
            reason="dry-run",
        )


if __name__ == "__main__":
    main()
