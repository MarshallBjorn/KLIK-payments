# RTGS Settlement — gdzie są pieniądze i jak się to rozlicza

Dokument wyjaśnia przepływ pieniędzy w KLIK: gdzie fizycznie leżą środki, jak
powstają zobowiązania, gdzie są **prowizje KLIK (nasze składki)**, jak działa
netting i jak sesje rozliczeniowe domykają się przez bramki RTGS.

> TL;DR: **KLIK nie trzyma pieniędzy.** Jest routerem autoryzacji i rejestrem
> zobowiązań. Realne pieniądze ruszają się między **kontami rozliczeniowymi
> banków w banku centralnym** (RTGS). KLIK liczy kto‑komu‑ile netto i zleca
> przelewy do RTGS.

---

## 1. Gdzie fizycznie są pieniądze

| Warstwa | Co trzyma | Przykład |
|---|---|---|
| Bank klienta / merchanta | realne salda klientów i sklepów | konto klienta w Banku N |
| **Konto rozliczeniowe banku w RTGS** | środki banku w banku centralnym | settlement account banku w SORBNET3/TARGET/CHAPS/FedNow |
| **KLIK** | **nic** — tylko rekordy zobowiązań (Postgres) | `LedgerEntry`, `SettlementTransfer` |

KLIK w żadnym momencie nie jest posiadaczem środków. Operuje na **zobowiązaniach**
("Bank A jest winien Bankowi B 148.05 PLN"), które potem są rozliczane jednym
przelewem netto przez RTGS.

---

## 2. Strefy i waluty (zone isolation)

Każda transakcja żyje w **jednej strefie** = jedna waluta = jeden RTGS:

| Strefa | Waluta | RTGS | Format komunikatu |
|---|---|---|---|
| PL | PLN | SORBNET3 | JSON (mock) |
| EU | EUR | TARGET | **ISO 20022 XML** (pain.001 → pain.002) |
| UK | GBP | CHAPS | JSON (mock) |
| US | USD | FedNow | JSON (mock) |

Cross‑zone jest odrzucane (`422_ZONE_MISMATCH`). Dzięki temu nigdy nie ma
przewalutowania w rozliczeniu — każda strefa rozlicza się u siebie.

---

## 3. Z czego składa się płatność C2B (split prowizji)

Przy `/payments/confirm` (ACCEPTED) kwota brutto dzieli się **od brutto**:

```
amount_gross = merchant_net + klik_fee + agent_fee
```

Przykład (brutto 150.00 PLN, klik_fee 0.30%, agent_fee 1.00%):

| Pozycja | Kwota | Komu |
|---|---|---|
| `klik_fee`     | 0.45  | **KLIK** (nasza składka) |
| `agent_fee`    | 1.50  | Agent (jego bank rozliczeniowy) |
| `merchant_net` | 148.05| Merchant (jego bank rozliczeniowy) |

Stawki czytane z `MSCAgreement` agenta — bank ich nie przesyła.

---

## 4. Jak powstają zobowiązania (LedgerEntry)

Źródło: `ledger/services/ledger_service.py::record_c2b_transaction`.

### Off‑us (nadawca i merchant w różnych bankach) → **3 entries**
| entry_type | from_bank → to_bank | beneficiary | kwota |
|---|---|---|---|
| `BANK_TO_BANK` | Bank N → Bank merchanta | BANK | `merchant_net` |
| `KLIK_FEE_C2B` | Bank N → Bank N* | **KLIK** | `klik_fee` |
| `AGENT_FEE`    | Bank N → Bank agenta | AGENT | `agent_fee` |

### On‑us (nadawca == bank merchanta) → **2 entries**
`merchant_net` pomijany (ruch wewnątrz jednego banku). Zostają `KLIK_FEE_C2B`
i `AGENT_FEE`.

\* Patrz §6 — `KLIK_FEE_C2B` ma `from_bank == to_bank` (collect‑at‑source).

Idempotencja: `source_ref = transaction.idempotency_key` — powtórny zapis tej
samej transakcji zwraca istniejące entries.

---

## 5. Prowizje P2P (nasze składki za lookup)

Źródło: `record_p2p_lookup_fees` (Celery Beat, codziennie 23:55 UTC).

- Każdy **udany** `GET /aliases/lookup/{phone}` (200) inkrementuje counter w Redis
  (`aliases:lookups:{bank_id}:YYYYMMDD`). Lookup 404 jest darmowy.
- Nocą: `total_fee = count × bank.p2p_lookup_fee` → `LedgerEntry`
  (`P2P_LOOKUP_FEE`, beneficiary **KLIK**), counter kasowany.
- Idempotencja: `source_ref = p2p:{bank_id}:{date}`.

Te entries trafiają do najbliższej sesji wspólnie z prowizjami C2B.

---

## 6. Gdzie są nasze składki i jak są inkasowane

KLIK pobiera prowizje (`KLIK_FEE_C2B`, `P2P_LOOKUP_FEE`) jako **uczestnik
rozliczeń** — ma własny bank-operator z kontem w RTGS danej strefy.

**Mechanizm (config‑driven, per strefa):** `settings.KLIK_OPERATOR_BANK_BY_ZONE`
mapuje strefę na `Bank.name` operatora KLIK:

```python
KLIK_OPERATOR_BANK_BY_ZONE = {
    'EU': 'KLIK Operator EU',
    # 'PL': 'KLIK Operator PL',  # dodawane wraz z onboardingiem KLIK w danym RTGS
}
```

- **Strefa W mapie** → `KLIK_FEE_C2B.to_bank = operator KLIK` (`from ≠ to`).
  Netting generuje realny `SettlementTransfer  Bank_N → KLIK`, dispatcher wysyła
  go do RTGS, a środki **realnie lądują na koncie KLIK** w danym systemie.
  To samo dla `P2P_LOOKUP_FEE`.
- **Strefa POZA mapą** → fallback **collect‑at‑source** (`to_bank = sender`,
  `from == to`): prowizja jest **tylko księgowana**, netuje się do zera, nie ma
  przelewu RTGS. (Stan przejściowy dla stref, w których KLIK nie jest jeszcze
  onboardowany.)

`merchant_net` i `agent_fee` mają realne `from ≠ to` zawsze — są rozliczane
przez RTGS niezależnie od powyższego.

> ⚠️ Konsekwencja operacyjna: w skonfigurowanej strefie **dostępność KLIK w RTGS
> gatekeep'uje sesję** — jeśli transfer prowizji do KLIK padnie, cała sesja
> strefy idzie `FAILED` (zasada „dowolny fail → sesja FAILED"). Dlatego strefę
> włącza się dopiero po onboardingu KLIK w jej RTGS.

**Status:** EU — inkaso aktywne i zweryfikowane end‑to‑end (saldo konta KLIK
w TARGET rośnie po sesji `COMPLETED`). PL/UK/US — collect‑at‑source do czasu
onboardingu KLIK w SORBNET3/CHAPS/FedNow.

---

## 7. Netting (multilateral) — z wielu zobowiązań do kilku przelewów

Źródło: `ledger/services/netting.py::net_obligations`.

1. Sumuje wszystkie zobowiązania sesji do **pozycji netto na uczestnika**
   (ile dany bank jest winien / ile ma dostać po skompensowaniu).
2. Greedy matching dłużników z wierzycielami → minimalna liczba przelewów.

Przykład z happy‑path (strefa EU):
```
Entries:  PL→DE 150,  DE→PL 200,  PL→FR 100
Netto:    PL = -50,   DE = -50,   FR = +100
Przelewy: PL→FR 50,   DE→FR 50          (3 zobowiązania → 2 przelewy)
```
Każdy wynik to jeden `SettlementTransfer` (kwota netto).

---

## 8. Cykl życia sesji rozliczeniowej

Źródło: `ledger/tasks.py::run_settlement_session` (Celery Beat, per strefa).

```
create_session(zone)            → SettlementSession OPEN
  └ assign_pending_entries      → entries.session = sesja,  status NETTING
      └ run_netting             → SettlementTransfer[],     status SETTLING
          └ RTGSDispatcher.dispatch(zone, transfers)   → bramka RTGS strefy
              └ mark_settled(results)                  → COMPLETED / FAILED
```

Stany: `SettlementSession: OPEN → NETTING → SETTLING → COMPLETED | FAILED`.

Zasady odporności:
- **Brak entries** → sesja `COMPLETED` z 0 przelewów (rekord dla audytu).
- **Wszystko zbilansowane wewnętrznie** (netto = 0) → `COMPLETED`, 0 przelewów.
- **RTGS całkowicie niedostępny** (`RTGSUnavailableError`) → cała sesja `FAILED`,
  entries zostają `settled=False` i wracają do następnej sesji.
- **Częściowy sukces** (część transferów FAIL) → sesja `FAILED`; entries między
  parami banków, których przelew się udał, są oznaczone `settled=True`, reszta
  wraca do puli. Operator widzi to w Django Admin.
- **Tylko jedna aktywna sesja na strefę** (`ActiveSessionExistsError`) — blokada
  przed double‑trigger (Beat × ręczny trigger).

> Uproszczenie `mark_settled`: po nettingu nie ma mapowania 1:1 entry→transfer,
> więc udany przelew `A→B` oznacza **wszystkie** niesettled entries między A i B
> w tej sesji jako rozliczone (`ledger_service.py::mark_settled`).

---

## 9. Dispatch do RTGS

Źródło: `ledger/rtgs/` (`dispatcher.py`, `gateways.py`).

- `RTGSDispatcher` (Strategy/Factory) wybiera bramkę po strefie i deleguje.
- Każdy `SettlementTransfer` idzie jako **osobny POST** (jeden fail nie wywala
  reszty — wymóg częściowego commitu).
- Pre‑flight `healthcheck()` — jeśli RTGS nie odpowiada, sesja `FAILED` jednym
  ruchem, bez bombardowania pojedynczymi transferami.
- Mapowanie wyniku: `SUCCESS` → `COMPLETED`, błąd biznesowy / HTTP≥400 →
  `FAILED` z `failure_reason`, timeout/sieć → `TIMEOUT`.
- `SettlementTransfer.rtgs_reference` = referencja nadana przez RTGS (dowód
  rozliczenia, do raportów dla banków).

Różnice formatów: TARGET używa ISO 20022 (XML pain.001 w żądaniu, pain.002 z
`TxSts=ACSC` w odpowiedzi). SORBNET3/CHAPS/FedNow w MVP rozmawiają JSON‑em.

---

## 10. Szybkie odpowiedzi

- **Gdzie są pieniądze?** Na kontach rozliczeniowych banków w RTGS. KLIK nie trzyma środków.
- **Jak płyną?** Bank→bank, jednym przelewem netto na strefę, po nettingu, przez RTGS danej strefy.
- **Gdzie nasze składki (KLIK)?** Księgowane jako `KLIK_FEE_C2B` / `P2P_LOOKUP_FEE` (beneficiary KLIK). W strefach z `KLIK_OPERATOR_BANK_BY_ZONE` **realnie inkasowane przez RTGS** (EU: tak). W pozostałych — collect‑at‑source (księgowane, nie pobierane).
- **Co realnie idzie przez RTGS?** `merchant_net` i `agent_fee` (from ≠ to).
- **Jak często?** Per strefa, w cyklu Celery Beat; P2P fee naliczane nocą (23:55 UTC).
- **Co przy awarii?** Nierozliczone entries wracają do następnej sesji (nic nie ginie).
