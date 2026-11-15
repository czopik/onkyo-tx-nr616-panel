# Changelog

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
