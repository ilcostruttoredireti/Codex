#!/usr/bin/env python3
"""
Gmail → HubSpot contact sync.

Polls Gmail for new INBOX messages and upserts the senders as HubSpot contacts.

Usage:
    python main.py                    # run continuously (60-second interval)
    python main.py --interval 120     # custom polling interval
    python main.py --once             # single pass, then exit
    python main.py --no-timeline      # skip HubSpot timeline notes
"""

import argparse
import logging
import os
import sys
import time

from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
)
logger = logging.getLogger(__name__)


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description='Gmail → HubSpot contact sync')
    p.add_argument('--interval', type=int, default=int(os.getenv('POLL_INTERVAL', '60')),
                   help='Polling interval in seconds (default: 60)')
    p.add_argument('--once', action='store_true',
                   help='Run a single sync cycle then exit')
    p.add_argument('--no-timeline', action='store_true',
                   help='Skip creating HubSpot timeline notes')
    return p.parse_args()


def _build_clients():
    hubspot_token = os.getenv('HUBSPOT_ACCESS_TOKEN', '').strip()
    if not hubspot_token:
        sys.exit('ERROR: HUBSPOT_ACCESS_TOKEN is not set. Copy .env.example to .env and fill it in.')

    credentials_file = os.getenv('GMAIL_CREDENTIALS_FILE', 'credentials.json')
    if not os.path.exists(credentials_file):
        sys.exit(
            f'ERROR: Gmail credentials file not found at "{credentials_file}". '
            'Download it from Google Cloud Console and set GMAIL_CREDENTIALS_FILE in .env.'
        )

    from gmail_client import GmailClient
    from hubspot_client import HubSpotClient

    gmail = GmailClient(
        credentials_file=credentials_file,
        token_file=os.getenv('GMAIL_TOKEN_FILE', 'token.json'),
    )
    hubspot = HubSpotClient(access_token=hubspot_token)
    return gmail, hubspot


def _print_summary(results) -> None:
    from sync import SyncStatus
    if not results:
        logger.info('Nessuna nuova email.')
        return

    counts = {s: 0 for s in SyncStatus}
    for r in results:
        counts[r.status] += 1

    logger.info(
        'Ciclo completato — %d email | Creati: %d | Aggiornati: %d | Ignorati: %d | Errori: %d',
        len(results),
        counts[SyncStatus.CREATED],
        counts[SyncStatus.UPDATED],
        counts[SyncStatus.SKIPPED],
        counts[SyncStatus.ERROR],
    )


def main() -> None:
    args = _parse_args()
    gmail, hubspot = _build_clients()

    from sync import run_sync_cycle

    logger.info('Gmail → HubSpot sync avviato (intervallo: %ds)', args.interval)

    while True:
        try:
            results = run_sync_cycle(gmail, hubspot, add_timeline=not args.no_timeline)
            _print_summary(results)
        except KeyboardInterrupt:
            logger.info('Interrotto dall\'utente.')
            break
        except Exception as exc:
            logger.error('Errore nel ciclo di sync: %s', exc, exc_info=True)

        if args.once:
            break

        time.sleep(args.interval)


if __name__ == '__main__':
    main()
