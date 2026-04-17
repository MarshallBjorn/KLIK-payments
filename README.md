# Specyfikacja Techniczna Systemu KLIK

## 1. Opis Projektu
System KLIK pełni rolę centralnego operatora i routera płatności mobilnych w ekosystemie bankowym. 
Zapewnia dwa główne moduły:
1. **KLIK Kody (C2B):** Autoryzacja płatności w punktach sprzedaży/sklepach internetowych za pomocą 6-cyfrowych kodów.
2. **KLIK Telefon (P2P):** Rejestr aliasów umożliwiający natychmiastowe przelewy międzybankowe na numer telefonu.

## 2. Wstępny opis działania
KLIK działa jako niezależny mikroserwis (Orkiestrator). Nie przechowuje środków pieniężnych, a jedynie zarządza logiką autoryzacji (Kody) oraz routingiem danych (Telefony). 
- **Kody:** Generowane przez KLIK na prośbę Banku, przechowywane w pamięci RAM z krótkim czasem życia.
- **Telefony:** Scentralizowana baza mapująca numer telefonu na parę: [ID_BANKU, NUMER_KONTA].

## 3. Stack Technologiczny
- **Framework API:** Django + Django REST Framework
- **Zarządzanie zadaniami (Webhooki):** Celery + RabbitMQ/Redis Broker
- **Baza dla Kodów (Krótkotrwała):** Redis (z mechanizmem TTL na 120s)
- **Baza dla Aliasów i Historii (Trwała):** PostgreSQL
- **Konteneryzacja:** Docker / docker-compose

## 3. Plan na rozdzielenie stref (Zone Isolation)
W celu uniknięcia problemów z przewalutowaniami i systemami SWIFT, KLIK wprowadza rygorystyczną izolację strefową:
- Transakcja jest procesowana **wyłącznie** w obrębie tej samej waluty i kraju (np. PL -> PL).
- System automatycznie odrzuca żądania, gdzie waluta nadawcy i odbiorcy jest różna.
- Identyfikacja strefy następuje po prefiksie banku lub kraju.

## 4. Mockup Endpointów - API KLIK

### A. Moduł KODÓW (C2B)
| Metoda | Endpoint | Opis |
| :--- | :--- | :--- |
| `POST` | `/api/v1/codes/generate` | Bank prosi o wygenerowanie kodu dla klienta. |
| `POST` | `/api/v1/payments/initiate` | Sklep/Bramka przesyła kod wpisany przez klienta. |
| `POST` | `/api/v1/payments/confirm` | Bank przesyła ostateczne potwierdzenie po autoryzacji w apce. |

### B. Moduł TELEFONÓW (P2P)
| Metoda | Endpoint | Opis |
| :--- | :--- | :--- |
| `POST` | `/api/v1/aliases/register` | Bank rejestruje numer telefonu i konto klienta w KLIK. |
| `GET` | `/api/v1/aliases/lookup/{phone}` | Bank nadawcy pyta, na jakie konto wysłać pieniądze. |
| `DELETE` | `/api/v1/aliases/{phone}` | Wyrejestrowanie usługi "Przelew na telefon". |

## 5. Endpointy BANKU (Webhooki - Wymagane od Banków)
Aby KLIK mógł działać, **KAŻDY BANK** musi wystawić u siebie poniższy endpoint, do którego KLIK będzie uderzał asynchronicznie:

### `POST /api/klik-webhook/v1/authorize`
**Payload wysyłany przez KLIK:**
```json
{
    "transaction_id": "uuid-string",
    "user_id": "string-id-klienta",
    "amount": "150.00",
    "currency": "PLN",
    "merchant_name": "Sklep Żabka",
    "expiry_time": "2023-10-27T10:00:00Z"
}
```
**Oczekiwanie**: Bank musi wyświetlić powiadomienie push klientowi i po jego akceptacji uderzyć do KLIKa na endpoint /payments/confirm.

## 6. Słownik błędów
- `404_CODE_EXPIRED`: Kod utracił ważność.
- `403_INSUFFICIENT_FUNDS`: Bank odrzucił transakcję z powodu braku środków (przekazane do KLIK).
- `409_ALIAS_ALREADY_EXISTS`: Numer telefonu jest już przypisany do innego konta.
- `422_ZONE_MISMATCH`: Próba transakcji między różnymi strefami walutowymi.

## 7. Bezpieczeństwo
1. **API Keys**: Uwierzytelnianie statycznym nagłówkiem `X-KLIK-Api-Key` przypisanym do konkretnego banku.
2. **Role-Based Access**: Endpointy `/codes/generate` oraz cały moduł `/aliases` mogą być wywoływane WYŁĄCZNIE przez autoryzowane systemy bankowe. Sklepy internetowe mają dostęp tylko do `/payments/initiate` i `/status`.
3. **Network Isolation**: Wykorzystanie wewnętrznej sieci platformy Docker do ograniczenia ruchu. KLIK akceptuje ruch zarządczy tylko z kontenerów znajdujących się w określonej podsieci / posiadających zaufane hostnamy.