# eISCP / DLNA — notatki protokołu (zweryfikowane na TX-NR616, firmware 2012)

Wszystko poniżej sprawdzone empirycznie na żywo, nie przepisane z dokumentacji.

## 1. eISCP (Ethernet, port TCP 60128)

### Ramka

```
49 53 43 50              "ISCP"
00 00 00 10              rozmiar nagłówka = 16 (stały)
LL LL LL LL              długość danych (payload) w big-endian
01 00 00 00              wersja
00 00 00 1D              reserved (3) + typ urządzenia 0x1D (amplituner)
<payload>                np. "!1PWRQSTN\r\n"
```

Payload **musi** mieć prefiks `!1` (strefa główna). Bez niego amplituner milczy.
Odpowiedź: `!1PWR01` + często `\x1a` przed `\r\n` — trzeba go obcinać.

### Odpowiedzi

- eISCP odpowiada na zapytania `QSTN` i na zmiany stanu.
- Urządzenie **rozgłasza** stan cyklicznie (PWR, AMT, LMD, MVL, SLI, DIM…),
  więc trzeba odróżniać echo komendy od broadcastu (patrz `optimistic_fields()` w `onkyo_web.py`).
- Jedno połączenie = jeden strumień; kilka równoległych połączeń działa (używamy dwóch:
  poller + komendy), ale przy zbyt szybkich reconnectach moduł sieciowy potrafi się zresetować.

### Obsługiwane komendy (na tym egzemplarzu)

| Kod | Znaczenie | Format wartości |
|---|---|---|
| `PWR` | zasilanie | `00` standby, `01` on, `ALL` wszystkie strefy |
| `MVL` | głośność | hex `00`–`64`; `UP`/`DOWN`; `QSTN` |
| `AMT` | mute | `00`/`01`, `TG` toggle |
| `SLI` | wejście | hex, np. `10` BD/DVD, `01` CBL/SAT, `00` STB/DVR, `02` GAME, `05` PC, `03` AUX, `25` AM, `24` FM, `23` TV/CD, `2B` NET, `29` USB |
| `LMD` | tryb nasłuchu | hex, np. `00` Stereo, `0C` All Ch Stereo, `86` PLIIx Game, `42` THX Cinema |
| `RAS` | Cinema Filter | `00`/`01` |
| `HDO` | wyjście HDMI | `00`–`05` |
| `DIM` | dimmer | `00` bright, `01` dim, `02` dark, `03` shut-off, `08` bright+LED off |
| `SLP` | sleep | minuty w hex (`1E` = 30 min, `3C` = 60, `5A` = 90), `OFF` |
| `DIF` | tryb wyświetlacza | `00`–`03` |
| `TFR` | tony front | `B`/`T` + znak + cyfra hex: `B00T00`, `B+4T00`, `B-6T00` (−10…+10 dB) |
| `SWL` | poziom subwoofera | `00`, `+2`, `-2` (−15…+12 dB) |
| `CTL` | poziom centralnego | `00`, `+2`, `-2` (±12 dB) |
| `TUN` | tuner | 5 cyfr: FM `08750` = 87,50 MHz, AM `00522` = 522 kHz; `UP`/`DOWN`/`BAND`/`DIRECT` |
| `PRS` | preset | `00`, `01`…; `UP`/`DOWN` |
| `ZPW`/`ZVL`/`ZMT`/`SLZ` | strefa 2 | jak PWR/MVL/AMT/SLI |
| `PW3`/`VL3`/`MT3`/`SL3` | strefa 3 | j.w. |
| `IFA` | info o sygnale | np. `HDMI 1,Dolby D +,48 kHz,5.1 ch,THX Cinema,5.1 ch,` |

### NIE obsługiwane (mimo że są w dokumentacji)

`ECO`, `CEC`, `SPA`/`SPB` (głośniki A/B), `TFH`/`TFW`/`TCT`/`TSW` (tony kanałów innych niż front),
`FWV` (wersja firmware), `SLC` (test ton), **`TCL`** oraz `FWL`/`FWR`/`SLL`/`SLR`/`SBL`/`SBR`
(poziomy front L/R i surround L/R). Ten model reguluje je tylko w menu OSD:
**SETUP → Speaker Setup → Level Calibration**.

> Uwaga: `SLI` w dokumentacji ma też kody VIDEO1–7, PHONO, TAPE, MULTI CH itd. —
> TX-NR616 ich nie ma, więc panel pokazuje tylko realne 11 wejść.

## 2. UPnP / DLNA (DMR, port 8888)

- Deskryptor: `http://IP:8888/upnp_descriptor_0`
- Usługi: `AVTransport`, `RenderingControl`, `ConnectionManager` (Stereo, Mediabolic 1.8)
- DMR-1.50, sink: LPCM 44,1/48 kHz, MP3, WMA, AAC, WAV, FLAC, OGG
- `SetAVTransportURI` + `Play` + `Stop` + `GetTransportInfo`/`GetPositionInfo` **działają**
- **Pauza i Seek nie są obsługiwane** — zwracają błąd UPnP (mimo obecności akcji w SCPD)
- `GetVolume` zwraca 0 niezależnie od stanu — do głośności używamy eISCP
- Poprawny cast: serwer HTTP na PC (port 8765, z obsługą `Range`) + `SetAVTransportURI`
  z metadanymi DIDL-Lite; amplituner sam przełącza wejście na NET i pobiera plik.

## 3. Wykrywanie urządzenia

1. SSDP `M-SEARCH *` na `239.255.255.250:1900` → `LOCATION` → deskryptor 8888
   → filtr `manufacturer ~ ONKYO` lub `modelName ~ TX-`.
2. Fallback: równoległy skan TCP portu 60128 w podsieci `/24`.
3. Wybrany host jest zapisywany w `config.json` (zmienna `ONKYO_CONFIG`).

## 4. Standby i sieć (ważne)

Po wejściu w standby ten model **wyłącza moduł sieciowy** (porty 80/8888/60128 znikają
w ~3 s), jeśli w menu amplitunera `Network Standby = Off`. Wtedy nie da się go obudzić
z sieci (ani tym panelem, ani aplikacją Onkyo). Rozwiązanie:
**SETUP → Network → Network Standby → On**. Po tym w standby sieć działa i `PWR01` włącza urządzenie.

## 5. Źródła

- `onkyo-eiscp` (miracle2k) — katalog komend i formatów YAML
- własne testy na TX-NR616 (firmware 2012), logi w historii projektu
