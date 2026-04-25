"""Testy healthcheck endpointa."""

import pytest
from rest_framework import status
from rest_framework.test import APIClient


@pytest.fixture
def client():
    return APIClient()


@pytest.mark.django_db
def test_healthz_returns_200(client):
    """Healthcheck powinien zwrócić 200 gdy DB i cache działają."""
    response = client.get('/healthz/')

    assert response.status_code == status.HTTP_200_OK
    assert response.data['status'] == 'healthy'


@pytest.mark.django_db
def test_healthz_includes_db_check(client):
    """Healthcheck powinien zawierać status bazy danych."""
    response = client.get('/healthz/')

    assert 'database' in response.data['checks']
    assert response.data['checks']['database']['ok'] is True


@pytest.mark.django_db
def test_healthz_includes_cache_check(client):
    """Healthcheck powinien zawierać status cache."""
    response = client.get('/healthz/')

    assert 'cache' in response.data['checks']
    assert response.data['checks']['cache']['ok'] is True
