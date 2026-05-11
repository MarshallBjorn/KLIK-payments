"""Django management command: ręczny trigger sesji rozliczeniowej.

Użycie:
    # Jedna strefa, sync (czeka na wynik)
    python manage.py run_settlement --zone PL

    # Wszystkie strefy, async (enqueue do Celery)
    python manage.py run_settlement --all

    # Tylko sprawdzenie health RTGS bez triggerowania
    python manage.py run_settlement --healthcheck

Praktyczne dla:
- Demo (operator triggeruje sesję na życzenie zamiast czekać na Beat).
- Recovery po awarii (jeśli Beat padł).
- Lokalnego smoke-testu pełnego pipeline.
"""

from django.core.management.base import BaseCommand

from common.enums import Zone
from ledger.rtgs import RTGSDispatcher
from ledger.tasks import run_settlement_all_zones, run_settlement_session


class Command(BaseCommand):
    help = 'Triggeruje sesję rozliczeniową (settlement) dla strefy lub wszystkich stref.'

    def add_arguments(self, parser):
        group = parser.add_mutually_exclusive_group(required=True)
        group.add_argument(
            '--zone',
            choices=[z.value for z in Zone],
            help='Triggeruje sesję dla jednej strefy (sync — czeka na wynik).',
        )
        group.add_argument(
            '--all',
            action='store_true',
            help='Triggeruje sesje dla wszystkich 4 stref (async, przez Celery).',
        )
        group.add_argument(
            '--healthcheck',
            action='store_true',
            help='Sprawdza dostępność wszystkich gateway-ów RTGS bez triggera.',
        )

    def handle(self, *args, **opts):
        if opts['healthcheck']:
            self._healthcheck()
            return

        if opts['all']:
            self._run_all()
            return

        if opts['zone']:
            self._run_one(opts['zone'])

    def _healthcheck(self):
        dispatcher = RTGSDispatcher.from_settings()
        results = dispatcher.healthcheck_all()
        for zone, healthy in results.items():
            symbol = '✓' if healthy else '✗'
            color = self.style.SUCCESS if healthy else self.style.ERROR
            self.stdout.write(color(f'  {symbol} {zone:4} {"OK" if healthy else "DOWN"}'))

    def _run_one(self, zone: str):
        self.stdout.write(f'Trigger settlement dla strefy {zone}...')
        # .apply() = sync (eager), nie czekamy na worker
        result = run_settlement_session.apply(args=[zone]).get()

        status = result.get('status', 'UNKNOWN')
        if status in ('COMPLETED', 'OK'):
            self.stdout.write(self.style.SUCCESS(f'OK: {result}'))
        elif status == 'SKIPPED':
            self.stdout.write(self.style.WARNING(f'SKIPPED: {result}'))
        else:
            self.stdout.write(self.style.ERROR(f'FAIL: {result}'))

    def _run_all(self):
        self.stdout.write('Enqueue settlement dla wszystkich stref (async)...')
        result = run_settlement_all_zones.delay().get(timeout=60)
        for zone, task_id in result.get('tasks', {}).items():
            self.stdout.write(f'  {zone}: {task_id}')
        self.stdout.write(self.style.SUCCESS('Enqueued. Sprawdź worker logs / admin.'))
