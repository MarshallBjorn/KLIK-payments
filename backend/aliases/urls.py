"""
URL routing dla apki aliases.

Zgodnie z dokumentacją (docs/c2b/integration/INFO.md):

    POST   /aliases/register
    GET    /aliases/lookup/<phone>
    DELETE /aliases/<phone>

Używamy konwertera `path` (string) bo phone w E.164 zawiera `+` — Django
przepuści go w path segmencie po URL-decode. Klient musi zapisać `+` jako
`%2B` zgodnie z RFC 3986 (`+` w path jest co prawda dozwolony jako sub-delim,
ale część klientów go reescape'uje).
"""

from django.urls import path

from aliases.views import alias_delete, alias_lookup, alias_register

app_name = 'aliases'

urlpatterns = [
    path('register', alias_register, name='register'),
    path('lookup/<str:phone>', alias_lookup, name='lookup'),
    path('<str:phone>', alias_delete, name='delete'),
]
