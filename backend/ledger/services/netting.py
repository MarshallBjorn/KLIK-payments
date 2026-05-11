"""
Netting service — algorytm multilateralnego nettingu z greedy matching.

Wejście: lista zobowiązań w postaci `(from_bank_id, to_bank_id, amount)`.
Wyjście: zminimalizowana lista przelewów RTGS osiągająca te same pozycje
netto u wszystkich uczestników.

Algorytm — dwa etapy:

ETAP 1 — Multilateralny netting (kalkulacja pozycji netto)

    Dla każdego uczestnika:  netto = SUM(należności) - SUM(zobowiązań)
    Dłużnicy:    netto < 0 (muszą zapłacić)
    Wierzyciele: netto > 0 (mają dostać)
    Suma wszystkich pozycji netto = 0 (sanity check).

ETAP 2 — Greedy matching (minimalizacja liczby przelewów)

    Sortujemy dłużników i wierzycieli po wartości bezwzględnej netto malejąco.
    W każdej iteracji największy dłużnik płaci największemu wierzycielowi
    `min(|debt|, credit)`. Jeden z nich zeruje się i wypada z puli.
    Powtarzamy do wyczerpania.

    Dla N uczestników: max N-1 przelewów (zwykle dużo mniej).

Algorytm jest greedy heuristic — nie daje gwarancji minimum globalnego (to NP-hard,
patrz "minimum cash flow problem"), ale praktycznie redukuje liczbę przelewów
o 60-90% i działa w O(N log N).

Operuje na czystych typach Python (UUID, Decimal) — bez zależności od ORM.
Łatwo testowalny w izolacji (patrz docs/c2b/diagrams/WORKFLOW.md, sekcja "Przykład").
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal
from uuid import UUID

logger = logging.getLogger('klik')


@dataclass(frozen=True)
class Obligation:
    """Pojedyncze zobowiązanie: from_bank musi zapłacić to_bank kwotę amount.

    Mapowanie z `LedgerEntry`:
        from_bank_id = entry.from_bank_id
        to_bank_id   = entry.to_bank_id
        amount       = entry.amount
    """

    from_bank_id: UUID
    to_bank_id: UUID
    amount: Decimal


@dataclass(frozen=True)
class NetTransfer:
    """Wynik nettingu — pojedynczy przelew RTGS po agregacji.

    Mapuje się 1:1 na `SettlementTransfer` przy zapisie do DB.
    """

    from_bank_id: UUID
    to_bank_id: UUID
    amount: Decimal


def calculate_net_positions(obligations: Iterable[Obligation]) -> dict[UUID, Decimal]:
    """Liczy pozycję netto każdego uczestnika.

    Dla bank_id:  netto = SUM(otrzyma) - SUM(zapłaci)
    netto > 0  → wierzyciel  (otrzyma w sumie)
    netto < 0  → dłużnik     (zapłaci w sumie)
    netto == 0 → zbilansowany (pomijamy w matchingu)

    Args:
        obligations: lista pojedynczych zobowiązań (z LedgerEntry).

    Returns:
        Dict bank_id → pozycja netto. Uczestnicy z netto == 0 są obecni
        w słowniku ale ignorowani przez matching.
    """
    positions: dict[UUID, Decimal] = {}

    for obl in obligations:
        # from_bank zapłaci → odejmujemy
        positions[obl.from_bank_id] = positions.get(obl.from_bank_id, Decimal('0')) - obl.amount
        # to_bank otrzyma → dodajemy
        positions[obl.to_bank_id] = positions.get(obl.to_bank_id, Decimal('0')) + obl.amount

    # Sanity check — w księdze podwójnej suma musi być 0. Jeśli nie jest, mamy
    # bug w czyimś rejestrze entries. Logujemy zamiast rzucać — i tak wartości
    # netto pozostają poprawne, settlement się wykona, ale ktoś musi to potem
    # rozdebugować ze stat-ami sesji.
    total = sum(positions.values(), Decimal('0'))
    if total != Decimal('0'):
        logger.error(
            'calculate_net_positions: SANITY CHECK FAILED — '
            'suma netto = %s (oczekiwano 0). Możliwy bug w ledger entries.',
            total,
        )

    return positions


def greedy_match(positions: dict[UUID, Decimal]) -> list[NetTransfer]:
    """Greedy matching dłużników z wierzycielami.

    Algorytm:
        1. Podziel uczestników na dłużników (netto < 0) i wierzycieli (netto > 0).
        2. Posortuj po |netto| malejąco.
        3. Iteracja: największy dłużnik płaci największemu wierzycielowi
           min(|debt|, credit). Co najmniej jeden z nich zeruje się.
        4. Zerujący wypada z puli, drugi zostaje z resztą.
        5. Powtarzaj aż obie pule puste.

    Złożoność: O(N log N) sortowania + O(N) iteracji main-loop = O(N log N).

    Args:
        positions: dict bank_id → pozycja netto (z calculate_net_positions).

    Returns:
        Lista NetTransfer — zminimalizowane przelewy RTGS.
        Pusta gdy wszystkie pozycje == 0.
    """
    # Rozdziel na listy [bank_id, abs(netto)] żeby móc mutować remaining amount.
    # Pomijamy uczestników z netto == 0 (już zbilansowani).
    debtors: list[list] = sorted(
        ([bank_id, -netto] for bank_id, netto in positions.items() if netto < 0),
        key=lambda x: x[1],
        reverse=True,
    )
    creditors: list[list] = sorted(
        ([bank_id, netto] for bank_id, netto in positions.items() if netto > 0),
        key=lambda x: x[1],
        reverse=True,
    )

    transfers: list[NetTransfer] = []
    d_idx = 0
    c_idx = 0

    while d_idx < len(debtors) and c_idx < len(creditors):
        debtor_id, debt_remaining = debtors[d_idx]
        creditor_id, credit_remaining = creditors[c_idx]

        # Ile faktycznie płynie w tym przelewie?
        amount = min(debt_remaining, credit_remaining)

        transfers.append(
            NetTransfer(
                from_bank_id=debtor_id,
                to_bank_id=creditor_id,
                amount=amount,
            )
        )

        # Aktualizujemy pozostałe kwoty
        debtors[d_idx][1] = debt_remaining - amount
        creditors[c_idx][1] = credit_remaining - amount

        # Kto się wyzerował, ten wypada
        if debtors[d_idx][1] == Decimal('0'):
            d_idx += 1
        if creditors[c_idx][1] == Decimal('0'):
            c_idx += 1

    return transfers


def net_obligations(obligations: Iterable[Obligation]) -> list[NetTransfer]:
    """Pełny pipeline: oblicz pozycje netto → greedy matching.

    Convenience wrapper. To jest TEN punkt wejścia używany przez worker.

    Args:
        obligations: lista zobowiązań (z LedgerEntry).

    Returns:
        Zminimalizowana lista NetTransfer do dispatchu RTGS.
    """
    obligations_list = list(obligations)

    if not obligations_list:
        logger.info('net_obligations: brak zobowiązań — no-op')
        return []

    positions = calculate_net_positions(obligations_list)
    transfers = greedy_match(positions)

    logger.info(
        'net_obligations: %d zobowiązań → %d uczestników → %d przelewów RTGS '
        '(redukcja %d→%d, %.0f%%)',
        len(obligations_list),
        len([p for p in positions.values() if p != 0]),
        len(transfers),
        len(obligations_list),
        len(transfers),
        (
            (1 - len(transfers) / len(obligations_list)) * 100
            if obligations_list
            else 0
        ),
    )

    return transfers
