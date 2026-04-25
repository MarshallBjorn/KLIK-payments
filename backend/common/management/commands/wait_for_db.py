"""
Management command — czeka aż baza danych będzie dostępna.
Używane w docker-compose żeby uniknąć race condition przy starcie.
"""

import time

from django.core.management.base import BaseCommand
from django.db import connections
from django.db.utils import OperationalError


class Command(BaseCommand):
    help = 'Pauses execution until database is available'

    def add_arguments(self, parser):
        parser.add_argument(
            '--max-retries',
            type=int,
            default=30,
            help='Maximum number of connection attempts',
        )
        parser.add_argument(
            '--delay',
            type=float,
            default=1.0,
            help='Delay in seconds between retries',
        )

    def handle(self, *args, **options):
        max_retries = options['max_retries']
        delay = options['delay']

        self.stdout.write('Waiting for database...')

        for attempt in range(1, max_retries + 1):
            try:
                connections['default'].ensure_connection()
                self.stdout.write(self.style.SUCCESS('Database available!'))
                return
            except OperationalError:
                self.stdout.write(
                    f'Database unavailable (attempt {attempt}/{max_retries}), '
                    f'waiting {delay}s...'
                )
                time.sleep(delay)

        self.stderr.write(self.style.ERROR(f'Database unavailable after {max_retries} attempts'))
        raise SystemExit(1)
