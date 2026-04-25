"""
Serializery DRF dla apki aliases.

- `AliasRegisterSerializer` — input dla POST /aliases/register
- `AliasRegisterResponseSerializer` — output 201
- `AliasLookupResponseSerializer` — output 200 dla GET /aliases/lookup/{phone}

Walidacja domenowa siedzi w `Alias.clean()` — serializer woła `full_clean`
pośrednio przez `model.save()`. Tutaj robimy tylko walidację formatu wejścia
(typy, wymagane pola).

Jeśli klient wysyła `iban` (tak jak w dokumentacji INFO.md), zamieniamy go na
strukturalne `account_identifier` żeby zachować spójność z modelem.
"""

from rest_framework import serializers

from aliases.models import Alias
from common.enums import Zone


class AliasRegisterSerializer(serializers.Serializer):
    """Input dla rejestracji aliasu.

    Body przykładowe (zgodne z docs/c2b/integration/INFO.md):
        {
            "phone": "+48501234567",
            "iban": "PL61109010140000071219812874",
            "zone": "PL"
        }

    Dla strefy US zamiast `iban` można podać `account_identifier` w formacie
    JSON (`{"type": "us_routing", "routing_number": "...", "account_number": "..."}`).
    """

    phone = serializers.CharField(max_length=16)
    zone = serializers.ChoiceField(choices=Zone.choices)

    # Dwa wzajemnie wykluczające się sposoby przekazania konta:
    iban = serializers.CharField(
        max_length=34,
        required=False,
        allow_blank=False,
        help_text='IBAN dla stref PL/EU/UK. Wzajemnie wykluczające się z account_identifier.',
    )
    account_identifier = serializers.JSONField(
        required=False,
        help_text='Strukturalny identyfikator konta (US, lub jawne podanie dla PL/EU/UK).',
    )

    def validate(self, attrs):
        iban = attrs.get('iban')
        account_identifier = attrs.get('account_identifier')

        if iban and account_identifier:
            raise serializers.ValidationError(
                'Podaj tylko jedno z pól: iban lub account_identifier.'
            )
        if not iban and not account_identifier:
            raise serializers.ValidationError(
                'Wymagane jedno z pól: iban (PL/EU/UK) lub account_identifier (US).'
            )

        # Normalizacja: jeśli klient podał `iban`, opakowujemy go w struktualną
        # postać żeby model widział tylko jeden format.
        if iban:
            attrs['account_identifier'] = {'type': 'iban', 'value': iban}
            attrs.pop('iban')

        return attrs


class AliasRegisterResponseSerializer(serializers.ModelSerializer):
    """Odpowiedź 201 — zwracamy id, phone i timestamp rejestracji."""

    alias_id = serializers.UUIDField(source='id', read_only=True)
    registered_at = serializers.DateTimeField(source='created_at', read_only=True)

    class Meta:
        model = Alias
        fields = ['alias_id', 'phone', 'registered_at']
        read_only_fields = fields


class AliasLookupResponseSerializer(serializers.ModelSerializer):
    """Odpowiedź 200 dla lookupu — dane potrzebne bankowi nadawcy do routingu.

    Wyciągamy `iban` z `account_identifier` dla wygody klienta (banki pytające
    przez API oczekują płaskiego `iban` zgodnie z INFO.md). Dla US zwracamy
    całe `account_identifier` jako fallback.
    """

    bank_id = serializers.UUIDField(source='bank.id', read_only=True)
    bank_code = serializers.CharField(source='bank.name', read_only=True)
    iban = serializers.SerializerMethodField()
    account_identifier = serializers.JSONField(read_only=True)

    class Meta:
        model = Alias
        fields = ['phone', 'bank_id', 'bank_code', 'iban', 'account_identifier']
        read_only_fields = fields

    def get_iban(self, obj: Alias) -> str | None:
        """Płaski IBAN dla stref PL/EU/UK (None dla US — patrz account_identifier)."""
        if obj.account_identifier.get('type') == 'iban':
            return obj.account_identifier.get('value')
        return None
