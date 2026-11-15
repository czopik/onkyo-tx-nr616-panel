# Changelog

## v1.1.1 — 2026-11-15

- mini-strona cast (`onkyo_cast.py ui`): **suwak głośności 0–80 → 0–100**
  (pełna skala `MVL`; poprzednio zakres 80–100 był nieosiągalny z tej strony)
- wersje serwerów podniesione do `1.1.1` (`onkyo-cast`, `onkyo-cast-ui`, `onkyo-web`)

## v1.1 — 2026-11-15

Wydanie „hardening + podkatalogi" (poprawki po przeglądzie zewnętrznym).

### Bezpieczeństwo serwera mediów (`onkyo_cast.py`)

- **ochrona przed Path Traversal**: zamiast `startswith()` używamy
  `os.path.realpath()` + `os.path.commonpath()` — nie da się wyjść poza katalog mediów
  przez `..%2f` ani przez bliźniacze nazwy katalogów (`media2/`)
- **walidacja nagłówka `Range`**: błędne/nieparsowalne zakresy i zakresy poza plikiem
  zwracają **HTTP 416** z `Content-Range: bytes */rozmiar`; poprawne dalej dają 206

### Podkatalogi i cast

- **rekurencyjne listowanie mediów** (`os.walk`) w panelu i w mini-stronie cast
- cast plików z zagnieżdżonych folderów (`Artysta/Album/utwór.flac`) — w `push_file()`
  zachowujemy ukośniki (`quote(..., safe='/')`), a walidacja ścieżki jest wspólna z serwerem
- poprawny MIME z rozszerzenia (m.in. `.flac`, `.m4a`) także dla plików w podkatalogach

### Wydajność i stabilność (`onkyo_web.py`)

- **I/O poza sekcją krytyczną**: listowanie plików i zapytania SOAP UPnP wykonują się
  przed `with self.lock:`, więc wątek WWW nie czeka na dysk/SOAP
- **timeout SOAP** skrócony z 10 s do **3 s** (szybszy powrót przy zerwaniu połączenia)
- **hybrydowy poller**: naprzemiennie pasywny nasłuch ramek rozgłaszanych przez amplituner
  (`listen(0.5)`) i rzadsze zapytania kontrolne (`batch()`), pełny odświeżacz co ~15 cykli
- `IFA` (informacja o sygnale) dodane do szybkiego odpytywania; uwzględniane też w `patch()`
- drobne utwardzenia: `_connect()` zamyka gniazdo przy błędzie `settimeout`,
  zamknięcie `cmd_link` przy wyjściu, bardziej tolerancyjny regex nazwy modelu w discovery

### Inne

- wersje serwerów podniesione do `1.1`
- odświeżona mini-strona `onkyo_cast.py ui` (cast z przeglądarki)

## v1.0 — 2026-11-15

Pierwsze publiczne wydanie. Zbudowane i przetestowane na żywo na Onkyo TX-NR616
(firmware 2012) + Unraid 7.2.2.

### Panel WWW (`onkyo_web.py`)

- dwie zakładki: **Główne** (zasilanie, głośność, źródła, „teraz gra", cast)
  i **Ustawienia** (połączenie, poziomy, tony, timery, strefy, tuner, konsola)
- **wskaźnik ONLINE/OFFLINE** + czas ostatniej odpowiedzi
- **wyszukiwanie amplitunera** (SSDP/UPnP + skan TCP 60128), zapis IP w `config.json`
- zwektoryzowany polling: osobne połączenie eISCP dla komend (reakcja ~40 ms)
- optymistyczne aktualizacje stanu (brak migotania starymi wartościami)
- lista źródeł ograniczona do realnych wejść TX-NR616
- linki do zakładek przez `#main` / `#settings`

### Sterowanie (eISCP)

- zasilanie, wejścia, tryby nasłuchu, głośność, mute, Cinema Filter
- tony bas/treble (front), poziom subwoofera i centralnego
- HDMI out, dimmer, sleep, tryb wyświetlacza
- strefy 2 i 3, tuner FM/AM (strojenie, direct, presety)
- konsola dowolnych komend eISCP

### Cast (DLNA DMR)

- serwer HTTP plików z obsługą `Range` + `SetAVTransportURI` / `Play` / `Stop`
- status transportu, cast z URL-a; potwierdzone formaty: LPCM, MP3, WMA, AAC, WAV, FLAC, OGG

### Wdrożenie

- `Dockerfile`, `docker-compose.yml`, szablon Unraid, launcher Windows
- GitHub Actions: walidacja składni Pythona, importów, szablonu HTML i JS

### Ustalenia protokołu (docs/PROTOCOL.md)

- eISCP wymaga prefiksu `!1`, odpowiedzi kończą się `\x1a`
- w standby z `Network Standby = Off` moduł sieciowy gaśnie (brak zdalnego wybudzania)
- ten model nie udostępnia przez sieć poziomów front L/R i surround L/R ani pauzy DLNA
