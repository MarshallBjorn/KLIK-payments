"""
AliasService — orkiestracja operacji P2P na rejestrze aliasów.

Trzy metody publiczne odpowiadające endpointom:

    register(bank, phone, account_identifier, zone)  → Alias
    lookup_for_bank(bank, phone)                      → Alias  (z inkrementem counter)
    unregister(bank, phone)                           → None

Service nie wie nic o HTTP — operuje na encjach domenowych. Mapowanie wyjątków
service → DRF APIException jest po stronie widoku.

Counter Redis:
- Każde POMYŚLNE `lookup_for_bank` inkrementuje 2 klucze:
  * `aliases:lookups:{querying_bank_id}:{YYYYMMDD}`  — dzienny licznik (TTL 35 dni)
  * `aliases:lookups:{querying_bank_id}:total`        — licznik kumulatywny
- Counter służy do (a) rozliczeń `p2p_lookup_fee` z T1, (b) dashboardów,
  (c) wykrywania anomalii (bank scrapuje rejestr).
- TTL 35 dni na dziennym kluczu — dłużej niż maksymalny cykl rozliczeniowy
  P2P (30 dni) z buforem na audyty.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import IntegrityError, transaction
from django_redis import get_redis_connection

from aliases.models import Alias
from aliases.services.exceptions import (
    AliasAlreadyRegisteredError,
    AliasDoesNotExistError,
    AliasOwnershipError,
    AliasZoneMismatchError,
)

if TYPE_CHECKING:
    from banks.models import Bank

logger = logging.getLogger("klik")

# Klucze Redis. Prefix `aliases:` żeby nie kolidować z `code:` / `tx:`.
LOOKUP_COUNTER_PREFIX = "aliases:lookups"

# 35 dni — z buforem ponad cykl rozliczeniowy P2P (30 dni).
DAILY_COUNTER_TTL_SECONDS = 35 * 24 * 60 * 60


class AliasService:
    """Operacje domenowe na rejestrze aliasów P2P."""

    def __init__(self):
        # Bezpośredni klient Redis dla atomowych INCR. django.core.cache nie
        # wystawia INCR — symetria z CodeService.
        self._redis = get_redis_connection("default")

    # ------------------------------------------------------------------
    # POST /aliases/register
    # ------------------------------------------------------------------

    def register(
        self,
        *,
        bank: Bank,
        phone: str,
        account_identifier: dict,
        zone: str,
    ) -> Alias:
        """Rejestruje alias telefon → konto bankowe.

        Args:
            bank: bank-właściciel (uwierzytelniony w widoku).
            phone: numer w E.164.
            account_identifier: strukturalny identyfikator konta.
            zone: strefa rejestracji (PL/EU/UK/US).

        Returns:
            Świeżo zapisana instancja Alias.

        Raises:
            AliasZoneMismatchError: prefiks ≠ zone lub zone ≠ bank.zone.
            AliasAlreadyRegisteredError: phone już istnieje (race condition
                lub jawny duplikat).
        """
        alias = Alias(
            phone=phone,
            bank=bank,
            account_identifier=account_identifier,
            zone=zone,
        )

        try:
            with transaction.atomic():
                # `Alias.save()` woła `full_clean()` → walidacja domenowa
                # (phone↔zone, zone↔bank.zone, account_identifier↔zone).
                alias.save()
        except DjangoValidationError as exc:
            # Rozpoznajemy zone mismatch po kluczu w message_dict — model
            # świadomie pakuje błędy strefowe pod kluczem 'zone'.
            error_dict = getattr(exc, "message_dict", {})
            if "zone" in error_dict:
                raise AliasZoneMismatchError(" ".join(error_dict["zone"])) from exc
            # Duplikat phone wykryty już na poziomie `validate_unique()`
            # w `full_clean()` (single-thread scenario). DB-level IntegrityError
            # poniżej obsługuje tylko race condition gdy dwa requesty przebiją
            # się przez validate_unique równolegle.
            if "phone" in error_dict:
                raise AliasAlreadyRegisteredError(phone) from exc
            # Inne błędy walidacji (np. account_identifier mismatch dla US)
            # propagujemy dalej — widok zmapuje na 400 przez DRF default.
            raise
        except IntegrityError as exc:
            # Race condition: dwa requesty z tym samym phone w tej samej chwili.
            # Pierwszy wygrywa, drugi dostaje IntegrityError → mapujemy na 409.
            if _is_unique_phone_violation(exc):
                raise AliasAlreadyRegisteredError(phone) from exc
            raise

        logger.info(
            "Zarejestrowano alias phone=%s bank=%s zone=%s",
            phone,
            bank.id,
            zone,
        )
        return alias

    # ------------------------------------------------------------------
    # GET /aliases/lookup/<phone>
    # ------------------------------------------------------------------

    def lookup_for_bank(self, *, querying_bank: Bank, phone: str) -> Alias:
        """Wyszukuje alias po numerze telefonu i inkrementuje counter.

        Args:
            querying_bank: bank pytający (nadawca przelewu P2P). Counter
                inkrementuje się DLA NIEGO — to on potem płaci `p2p_lookup_fee`.
            phone: numer w E.164.

        Returns:
            Instancja Alias z prefetchem `bank` (do serializacji bank_id/code).

        Raises:
            AliasDoesNotExistError: phone nie istnieje.

        Counter:
            Inkrementowany TYLKO przy znalezieniu aliasu (200). Miss-y (404)
            nie podlegają opłacie — bank nie płaci za nieudany lookup.
        """
        try:
            alias = Alias.objects.select_related("bank").get(phone=phone)
        except Alias.DoesNotExist as exc:
            raise AliasDoesNotExistError(phone) from exc

        # Inkrementacja PO znalezieniu (zgodnie z T2: "z inkrementem counter'a
        # w Redisie po 200"). Robimy fail-soft: jeśli Redis pada, lookup nadal
        # zwraca dane — ale logujemy WARNING, bo brak counter = brak rozliczeń.
        try:
            self._increment_lookup_counter(querying_bank.id)
        except Exception:  # noqa: BLE001
            # Szeroki except świadomie — jakikolwiek problem z Redisem
            # (connection error, timeout, OOM) nie powinien zwalić lookup-u.
            logger.exception(
                "Nie udało się zainkrementować counter lookup-u dla bank=%s phone=%s",
                querying_bank.id,
                phone,
            )

        return alias

    # ------------------------------------------------------------------
    # DELETE /aliases/<phone>
    # ------------------------------------------------------------------

    def unregister(self, *, bank: Bank, phone: str) -> None:
        """Usuwa alias — tylko jeśli `bank` jest jego właścicielem.

        Args:
            bank: bank wywołujący usunięcie.
            phone: numer aliasu do usunięcia.

        Raises:
            AliasDoesNotExistError: phone nie istnieje.
            AliasOwnershipError: bank nie jest właścicielem.
        """
        try:
            alias = Alias.objects.get(phone=phone)
        except Alias.DoesNotExist as exc:
            raise AliasDoesNotExistError(phone) from exc

        if alias.bank_id != bank.id:
            # Świadomie nie zwracamy 404 zamiast 403 (jak czasem robi się
            # dla obfuscation) — bank wie że alias istnieje (zna numer
            # telefonu klienta), więc 404 nic by nie ukryło.
            raise AliasOwnershipError(phone=phone, bank_id=bank.id)

        alias.delete()
        logger.info("Usunięto alias phone=%s bank=%s", phone, bank.id)

    # ------------------------------------------------------------------
    # Counter — read API dla testów/rozliczeń
    # ------------------------------------------------------------------

    def get_lookup_count(self, bank_id, day: str | None = None) -> int:
        """Zwraca licznik lookupów banku z danego dnia (lub total jeśli day=None).

        Args:
            bank_id: UUID banku.
            day: 'YYYYMMDD' lub None dla licznika kumulatywnego.

        Returns:
            Liczba lookupów (0 jeśli klucz nie istnieje).
        """
        key = self._daily_key(bank_id, day) if day else self._total_key(bank_id)
        value = self._redis.get(key)
        return int(value) if value is not None else 0

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _increment_lookup_counter(self, bank_id) -> None:
        """Atomowo inkrementuje dzienny i kumulatywny licznik lookupów.

        Pipeline w jednym round-tripie do Redisa. TTL ustawiamy tylko na
        dziennym kluczu — kumulatywny żyje w nieskończoność (do flush DB).
        """
        today = datetime.now(UTC).strftime("%Y%m%d")
        daily_key = self._daily_key(bank_id, today)
        total_key = self._total_key(bank_id)

        pipe = self._redis.pipeline()
        pipe.incr(daily_key)
        # TTL ustawiamy każdorazowo (`expire`, nie `expireat`) — Redis odświeża
        # TTL na każdy INCR. Dla 35-dniowego okna to akceptowalne, bo i tak
        # liczymy w cyklu miesięcznym z buforem.
        pipe.expire(daily_key, DAILY_COUNTER_TTL_SECONDS)
        pipe.incr(total_key)
        pipe.execute()

    @staticmethod
    def _daily_key(bank_id, day: str) -> str:
        return f"{LOOKUP_COUNTER_PREFIX}:{bank_id}:{day}"

    @staticmethod
    def _total_key(bank_id) -> str:
        return f"{LOOKUP_COUNTER_PREFIX}:{bank_id}:total"


# ----------------------------------------------------------------------
# Module-level helpers
# ----------------------------------------------------------------------


def _is_unique_phone_violation(exc: IntegrityError) -> bool:
    """Heurystyka: czy IntegrityError dotyczy unique constraint na phone.

    Patrzymy po nazwie constraintu z `Alias.Meta.constraints` — czysciej niż
    matchowanie generycznych komunikatów Postgres.
    """
    msg = str(exc).lower()
    return "alias_phone_unique" in msg or "phone" in msg
