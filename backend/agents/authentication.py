"""Custom DRF authentication dla agenta."""

import hashlib

from rest_framework import authentication, exceptions

from agents.models import Agent

AGENT_API_KEY_HEADER = 'X-KLIK-Agent-Api-Key'  # pragma: allowlist secret


class XKlikAgentApiKeyAuthentication(authentication.BaseAuthentication):
    """
    Authentication przez nagłówek X-KLIK-Agent-Api-Key.
    Hashuje klucz, szuka aktywnego agenta w bazie.
    """

    def authenticate(self, request):
        api_key = request.META.get('HTTP_X_KLIK_AGENT_API_KEY')

        if not api_key:
            return None

        api_key_hash = hash_api_key(api_key)

        try:
            agent = Agent.objects.select_related('settlement_bank').get(
                api_key_hash=api_key_hash,
            )
        except Agent.DoesNotExist:
            raise exceptions.AuthenticationFailed('Niepoprawny klucz API agenta.') from None

        if not agent.active:
            raise exceptions.AuthenticationFailed('Agent jest nieaktywny.')

        return (agent, None)

    def authenticate_header(self, request):
        return AGENT_API_KEY_HEADER


def hash_api_key(api_key: str) -> str:
    """Hash klucza API (SHA-256)."""
    return hashlib.sha256(api_key.encode('utf-8')).hexdigest()


def generate_api_key() -> tuple[str, str]:
    """
    Generuje nowy klucz API i jego hash.

    Returns:
        (plaintext_key, hash) — plaintext do jednorazowego pokazania,
        hash do zapisu w bazie.
    """

    import secrets

    plaintext = f'agent_{secrets.token_urlsafe(40)}'
    return plaintext, hash_api_key(plaintext)
