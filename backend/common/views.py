"""Healthcheck endpoints dla orkiestratora (Docker, K8s)."""

from django.core.cache import cache
from django.db import connections
from django.db.utils import OperationalError
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny
from rest_framework.response import Response


@api_view(['GET'])
@permission_classes([AllowAny])
def healthz(request):
    """
    Sprawdza dostępność DB i Redis.
    Zwraca 200 jeśli wszystko OK, 503 jeśli któryś z deps padł.
    """
    checks = {
        'database': _check_database(),
        'cache': _check_cache(),
    }

    is_healthy = all(check['ok'] for check in checks.values())
    http_status = status.HTTP_200_OK if is_healthy else status.HTTP_503_SERVICE_UNAVAILABLE

    return Response(
        {
            'status': 'healthy' if is_healthy else 'unhealthy',
            'checks': checks,
        },
        status=http_status,
    )


def _check_database():
    try:
        connections['default'].cursor().execute('SELECT 1')
        return {'ok': True}
    except OperationalError as e:
        return {'ok': False, 'error': str(e)}


def _check_cache():
    try:
        cache.set('healthcheck', 'ping', timeout=1)
        result = cache.get('healthcheck')
        if result != 'ping':
            return {'ok': False, 'error': 'Cache returned unexpected value'}
        return {'ok': True}
    except Exception as e:
        return {'ok': False, 'error': str(e)}
