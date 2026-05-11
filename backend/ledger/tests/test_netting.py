"""Testy algorytmu nettingu — czysta logika, bez Django/DB.

Pokrycie:
- calculate_net_positions — bilans, multilateral aggregation
- greedy_match — minimalizacja liczby przelewów, edge cases
- net_obligations — pełny pipeline (przykład z dokumentacji A5)
"""

from __future__ import annotations

from decimal import Decimal
from uuid import UUID

from ledger.services.netting import (
    NetTransfer,
    Obligation,
    calculate_net_positions,
    greedy_match,
    net_obligations,
)


def _u(seed: int) -> UUID:
    """Deterministyczne UUIDy dla łatwego śledzenia w testach."""
    return UUID(int=seed)


# ----------------------------------------------------------------------
# calculate_net_positions
# ----------------------------------------------------------------------


class TestCalculateNetPositions:
    def test_empty_input_returns_empty_dict(self):
        assert calculate_net_positions([]) == {}

    def test_single_obligation(self):
        a, b = _u(1), _u(2)
        result = calculate_net_positions(
            [Obligation(from_bank_id=a, to_bank_id=b, amount=Decimal("100"))]
        )
        assert result[a] == Decimal("-100")
        assert result[b] == Decimal("100")

    def test_bilateral_offset(self):
        """A→B 100 i B→A 30 → A netto -70, B netto +70."""
        a, b = _u(1), _u(2)
        result = calculate_net_positions(
            [
                Obligation(from_bank_id=a, to_bank_id=b, amount=Decimal("100")),
                Obligation(from_bank_id=b, to_bank_id=a, amount=Decimal("30")),
            ]
        )
        assert result[a] == Decimal("-70")
        assert result[b] == Decimal("70")

    def test_balanced_sum_to_zero(self):
        """Sanity check: w księdze podwójnej suma netto = 0."""
        a, b, c = _u(1), _u(2), _u(3)
        result = calculate_net_positions(
            [
                Obligation(from_bank_id=a, to_bank_id=b, amount=Decimal("500")),
                Obligation(from_bank_id=c, to_bank_id=a, amount=Decimal("200")),
                Obligation(from_bank_id=b, to_bank_id=c, amount=Decimal("300")),
            ]
        )
        assert sum(result.values()) == Decimal("0")

    def test_self_transfer_is_neutral(self):
        """A→A nie zmienia pozycji A (dodaje i odejmuje to samo)."""
        a = _u(1)
        result = calculate_net_positions(
            [Obligation(from_bank_id=a, to_bank_id=a, amount=Decimal("50"))]
        )
        assert result[a] == Decimal("0")


# ----------------------------------------------------------------------
# greedy_match
# ----------------------------------------------------------------------


class TestGreedyMatch:
    def test_empty_positions_returns_empty(self):
        assert greedy_match({}) == []

    def test_all_zeros_returns_empty(self):
        a, b = _u(1), _u(2)
        assert greedy_match({a: Decimal("0"), b: Decimal("0")}) == []

    def test_simple_two_party_match(self):
        a, b = _u(1), _u(2)
        result = greedy_match({a: Decimal("-100"), b: Decimal("100")})
        assert result == [
            NetTransfer(from_bank_id=a, to_bank_id=b, amount=Decimal("100"))
        ]

    def test_one_debtor_two_creditors(self):
        """Dłużnik 100; wierzyciele 70 i 30 → 2 przelewy."""
        debtor = _u(1)
        c1 = _u(2)
        c2 = _u(3)
        result = greedy_match(
            {debtor: Decimal("-100"), c1: Decimal("70"), c2: Decimal("30")}
        )
        # Sortowanie malejące: c1 (70) > c2 (30). Debtor płaci najpierw c1.
        assert result == [
            NetTransfer(from_bank_id=debtor, to_bank_id=c1, amount=Decimal("70")),
            NetTransfer(from_bank_id=debtor, to_bank_id=c2, amount=Decimal("30")),
        ]

    def test_two_debtors_one_creditor(self):
        """Dłużnicy -60 i -40; wierzyciel +100 → 2 przelewy."""
        d1 = _u(1)
        d2 = _u(2)
        creditor = _u(3)
        result = greedy_match(
            {d1: Decimal("-60"), d2: Decimal("-40"), creditor: Decimal("100")}
        )
        # d1 (60) > d2 (40), więc d1 idzie pierwszy
        assert result == [
            NetTransfer(from_bank_id=d1, to_bank_id=creditor, amount=Decimal("60")),
            NetTransfer(from_bank_id=d2, to_bank_id=creditor, amount=Decimal("40")),
        ]

    def test_complex_case_minimizes_transfers(self):
        """4 uczestników, każdy z różną pozycją netto. Greedy redukuje do 3 transferów."""
        a = _u(1)  # -265
        b = _u(2)  # +290
        c = _u(3)  # -55
        klik = _u(4)  # +30
        positions = {
            a: Decimal("-265"),
            b: Decimal("290"),
            c: Decimal("-55"),
            klik: Decimal("30"),
        }
        result = greedy_match(positions)

        # Z dokumentacji (przykład A5):
        # - a płaci b min(265, 290) = 265 → b zostaje +25
        # - c płaci b min(55, 25) = 25 → c zostaje -30, b zero
        # - c płaci klik min(30, 30) = 30 → wszystko zero
        assert len(result) == 3
        assert (
            NetTransfer(from_bank_id=a, to_bank_id=b, amount=Decimal("265")) in result
        )
        assert NetTransfer(from_bank_id=c, to_bank_id=b, amount=Decimal("25")) in result
        assert (
            NetTransfer(from_bank_id=c, to_bank_id=klik, amount=Decimal("30")) in result
        )

    def test_zero_position_skipped(self):
        a, b, c = _u(1), _u(2), _u(3)
        result = greedy_match({a: Decimal("-50"), b: Decimal("0"), c: Decimal("50")})
        # b nie powinien się pojawić w żadnym transferze
        for transfer in result:
            assert transfer.from_bank_id != b
            assert transfer.to_bank_id != b

    def test_decimal_precision_preserved(self):
        """Algorytm musi zachować precyzję Decimal (nie konwertować na float)."""
        a, b = _u(1), _u(2)
        result = greedy_match({a: Decimal("-148.05"), b: Decimal("148.05")})
        assert result[0].amount == Decimal("148.05")
        assert isinstance(result[0].amount, Decimal)


# ----------------------------------------------------------------------
# net_obligations — pełny pipeline (przykład z dokumentacji)
# ----------------------------------------------------------------------


class TestNetObligationsExample:
    """Pełen przykład z docs/c2b/diagrams/WORKFLOW.md (sekcja A5).

    Setup: 4 uczestników, 7 ledger entries → 3 przelewy RTGS.
    """

    def test_documentation_example(self):
        bank_a = _u(1)
        bank_b = _u(2)
        bank_c = _u(3)
        klik = _u(4)

        obligations = [
            Obligation(from_bank_id=bank_a, to_bank_id=bank_b, amount=Decimal("500")),
            Obligation(from_bank_id=bank_a, to_bank_id=bank_c, amount=Decimal("50")),
            Obligation(from_bank_id=bank_a, to_bank_id=klik, amount=Decimal("15")),
            Obligation(from_bank_id=bank_b, to_bank_id=bank_a, amount=Decimal("200")),
            Obligation(from_bank_id=bank_b, to_bank_id=klik, amount=Decimal("10")),
            Obligation(from_bank_id=bank_c, to_bank_id=bank_a, amount=Decimal("100")),
            Obligation(from_bank_id=bank_c, to_bank_id=klik, amount=Decimal("5")),
        ]

        result = net_obligations(obligations)

        # Z dokumentacji: dokładnie 3 przelewy
        assert len(result) == 3

        # Sprawdzenie konkretnych przelewów (dokumentacja pokazuje te wartości)
        assert (
            NetTransfer(from_bank_id=bank_a, to_bank_id=bank_b, amount=Decimal("265"))
            in result
        )
        assert (
            NetTransfer(from_bank_id=bank_c, to_bank_id=bank_b, amount=Decimal("25"))
            in result
        )
        assert (
            NetTransfer(from_bank_id=bank_c, to_bank_id=klik, amount=Decimal("30"))
            in result
        )

    def test_empty_obligations(self):
        assert net_obligations([]) == []

    def test_fully_balanced_obligations_yields_no_transfers(self):
        """A→B 100 i B→A 100 → wszystko się neutralizuje, 0 przelewów."""
        a, b = _u(1), _u(2)
        result = net_obligations(
            [
                Obligation(from_bank_id=a, to_bank_id=b, amount=Decimal("100")),
                Obligation(from_bank_id=b, to_bank_id=a, amount=Decimal("100")),
            ]
        )
        assert result == []

    def test_reduction_ratio_significant_for_large_input(self):
        """Smoke test: 100 zobowiązań między 5 bankami → << 100 przelewów."""
        banks = [_u(i) for i in range(5)]
        obligations = []
        for i in range(100):
            from_b = banks[i % 5]
            to_b = banks[(i + 1) % 5]
            obligations.append(
                Obligation(from_bank_id=from_b, to_bank_id=to_b, amount=Decimal("10"))
            )
        result = net_obligations(obligations)
        # 5 uczestników → max 4 przelewy. Dużo mniej niż 100.
        assert len(result) <= 4
