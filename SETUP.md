# Sett opp den autonome Polymarket-desken på din PC

Dette er hele oppsettet. Følg stegene i rekkefølge. Ikke hopp til live
før paper-kjøring har gått minst noen dager uten rare ordre.

Tidsbruk første gang: 20–40 minutter.

---

## 0. Hva du ender opp med

Et program som hvert 15. minutt:

1. Scanner likvide Polymarket-markeder
2. Lar Grok estimere sannsynlighet
3. Handler **kun** hvis netto edge ≥ 8 % etter spread, fee og 5 pp hårkutt
4. Aldri mer enn 6 % av bankroll per posisjon
5. Stopper automatisk ved daglig −4 % / ukentlig −10 %, eller hvis du lager en `HALT`-fil

To modus:

| Modus | `DRY_RUN` | Hva som skjer |
|---|---|---|
| Paper | `true` | Logger tenkte handler i `data/desk.db`. **Ingen** ekte ordre |
| Live | `false` | Sender limit-kjøp til Polymarket med din hot-wallet |

Start **alltid** i paper.

---

## 1. Ting du trenger før koden

### A. Python 3.11 eller nyere

**Windows**

1. Gå til https://www.python.org/downloads/
2. Last ned 3.12 (eller 3.13)
3. Kjør installeren
4. **Huk av** «Add python.exe to PATH»
5. Åpne PowerShell og sjekk:

```powershell
python --version
```

Du skal se `Python 3.12.x` eller nyere.

**Mac**

```bash
brew install python@3.12
python3 --version
```

**Linux**

```bash
sudo apt update && sudo apt install -y python3 python3-venv python3-pip
python3 --version
```

### B. xAI API-nøkkel (hjernen)

1. Gå til https://console.x.ai
2. Logg inn med X/xAI-kontoen din
3. Opprett en API-nøkkel
4. Kopier den. Den starter med `xai-`
5. Behold den. Du limer den inn i `.env` senere

Uten denne nøkkelen scanner agenten, men tar **ingen** veddemål.

### C. Dedikert Polymarket-wallet (penger)

Bruk **ikke** hovedwallet. Lag en slank hot-wallet som bare denne boten får.

Anbefalt (enklest for bot):

1. Installer [Rabby](https://rabby.io) eller MetaMask i Chrome
2. **Opprett ny wallet** (ikke importer den du bruker til alt annet)
3. Skriv ned seed på papir. Lås den vekk. Denne wallet brukes kun til boten
4. Kopier adressen (starter med `0x…`) — dette er både signer og `POLYMARKET_FUNDER` hvis du bruker EOA
5. Eksporter **private key**:
   - MetaMask: Konto → tre prikker → Account details → Show private key
   - Rabby: More → Export Private Key
6. Private key er 64 hex-tegn, ofte med `0x` foran. Du limer den inn i `.env`

Så fyller du den med et beløp du tåler å tape:

1. Gå til https://polymarket.com og logg inn **med denne nye wallet**
2. Deposit USDC.e / pUSD på **Polygon** (chain 137)
3. Sett inn det begrensede beløpet du har bestemt (f.eks. 200–1000 USD)
4. Gjør **én manuell mini-handel** i UI (f.eks. $1). Det setter allowances som API-en trenger
5. I Polymarket: Profile / Deposit — bekreft at saldoen ligger på samme `0x`-adresse som du kopierte

Hvis du i stedet logger inn med **e-post/Google** på Polymarket (ikke MetaMask):

- `SIGNATURE_TYPE=1`
- `POLYMARKET_FUNDER` = **deposit-adressen** vist på Polymarket (proxy), ikke nødvendigvis signer
- Private key eksporteres fra Polymarket (Settings → Export private key)

Hvis du er usikker: bruk ny MetaMask/Rabby, `SIGNATURE_TYPE=0`, og sett `POLYMARKET_FUNDER` lik wallet-adressen.

---

## 2. Last ned koden

Bruk **kun** GitHub-repoet `Rancowski/polymarket-desk` (eller `artifacts\polymarket-agent` fra Grok-zip). Ikke bland dem med nettside-zipen (`AGENTS.md`, `vite`, `src`).

Mappen skal inneholde `main.py`, `requirements.txt` og mappen `agent/`.

```powershell
cd $HOME
git clone https://github.com/Rancowski/polymarket-desk.git
cd polymarket-desk
```

Hvis repoet er privat, last ned zip fra GitHub og pakk ut.

---

## 3. Installer avhengigheter

Åpne PowerShell **i prosjektmappen**. Ikke kjør `Activate.ps1` — kall venv-python direkte.

**Windows:**

```powershell
cd C:\Users\DITTNAVN\polymarket-desk
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

**Mac / Linux:**

```bash
cd ~/polymarket-desk
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements.txt
```

Når det er ferdig skal du ha `py-clob-client`, `requests`, `python-dotenv` og `web3`.

---

## 4. Fyll inn `.env`

**Windows:**

```powershell
copy .env.example .env
notepad .env
```

**Mac / Linux:**

```bash
cp .env.example .env
nano .env
```

Lim inn verdiene. Ferdig fil ser slik ut (dine verdier, ikke disse):

```
DRY_RUN=true
POLYMARKET_PRIVATE_KEY=0xDIN_PRIVATE_KEY
POLYMARKET_FUNDER=0xDIN_WALLET_ADRESSE
SIGNATURE_TYPE=0
CHAIN_ID=137
POLY_API_KEY=
POLY_API_SECRET=
POLY_API_PASSPHRASE=
XAI_API_KEY=xai-DIN_NØKKEL
GROK_MODEL=grok-4.6
PAPER_BANKROLL_USD=1000
```

`PAPER_BANKROLL_USD` skal være det beløpet du later som (paper) eller har satt inn (live). Resten av tallene er allerede satt til reglene du ba om (6 % posisjon, 8 % edge).

Lagre og lukk. **Ikke lim nøkler inn i Grok-chat.**

---

## 5. Generer Polymarket API-credentials (én gang)

```powershell
.\.venv\Scripts\python.exe -m agent.bootstrap_creds
```

Den printer tre linjer:

```
POLY_API_KEY=...
POLY_API_SECRET=...
POLY_API_PASSPHRASE=...
```

Lim dem inn i `.env` og lagre.

Hvis den klager på private key: sjekk at `.env` ligger i **samme mappe som `main.py`**, og at nøkkelen starter med `0x` uten anførselstegn eller mellomrom.

---

## 6. Første paper-syklus (må lykkes før live)

```powershell
.\.venv\Scripts\python.exe main.py once
```

Dette kjører **én** runde: scanner → Grok estimerer → risk sier ja/nei → paper-fills logges.

Forventet i terminalen:

- `Syklus start dry_run=True bankroll=...`
- `Scout: N kandidater etter filter`
- `Brain: estimat for N/M markeder`
- Enten `PAPER BUY ...` eller mange `reject` med `edge_net ... < 0.08`

De fleste markeder **skal** avvises. 8 % netto edge er strengt. Det er meningen.

Se status:

```powershell
.\.venv\Scripts\python.exe main.py status
```

Logg og posisjoner ligger i `data/desk.db` (SQLite). Du kan åpne den med [DB Browser for SQLite](https://sqlitebrowser.org).

---

## 7. La den kjøre autonomt i paper

```powershell
.\.venv\Scripts\python.exe main.py run
```

Den looper hvert 15. minutt (`LOOP_SECONDS=900`). La vinduet stå åpent. Lukk = stopper.

La den gå **minst 2–3 dager i paper** før live. Se etter:

- At Grok svarer (ikke «XAI_API_KEY mangler»)
- At rejects har fornuftige grunner (`confidence=low`, `edge_net`, `spread`)
- At ingen enkelt paper-posisjon overstiger ~6 % av bankroll
- At den hopper over 5/15-min crypto up/down

Stopp med `Ctrl+C`.

---

## 8. Live (når paper ser sunt ut)

1. Bekreft at hot-wallet har det begrensede beløpet på Polymarket
2. Bekreft at du har gjort minst én manuell handel med den wallet
3. Sett i `.env`:

```
DRY_RUN=false
PAPER_BANKROLL_USD=DET_DU_FAKTISK_HAR_SATT_INN
```

4. Kjør én live-syklus først, ikke loop:

```powershell
.\.venv\Scripts\python.exe main.py once
```

5. Se i terminalen etter `LIVE ORDER` eller `reject`. Sjekk https://polymarket.com/portfolio at eventuelle ordre faktisk ligger der
6. Hvis det ser riktig ut:

```powershell
.\.venv\Scripts\python.exe main.py run
```

Nå handler den alene innenfor reglene.

---

## 9. Nødstopp

I prosjektmappen:

**Windows**

```powershell
New-Item -ItemType File -Name HALT
```

**Mac / Linux**

```bash
touch HALT
```

Neste syklus gjør **ingenting**. Slett `HALT` for å starte igjen.

`Ctrl+C` i terminalen stopper prosessen med en gang.

---

## 10. Hold den i gang 24/7 (valgfritt)

PC-en må være på, og du må ikke lukke terminalen. Alternativer:

### A. La PowerShell stå åpen
Enklest. Skjermsparer OK. Ikke slå av PC.

### B. Docker (hvis du har Docker Desktop)

```powershell
docker compose up -d --build
docker compose logs -f
```

Stopp:

```powershell
docker compose down
```

`HALT` med Docker: lag filen `HALT` i prosjektmappen og restart, eller `docker compose down`.

### C. Linux VPS (Hetzner/etc.) — best for ekte 24/7

Kopier prosjektet til `/opt/polymarket-desk`, installer som i steg 3, og:

```bash
sudo cp deploy/polymarket-desk.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now polymarket-desk
sudo journalctl -u polymarket-desk -f
```

---

## 11. Hva reglene faktisk gjør

Kodede hard-regler (kan endres i `.env`, ikke i hodet midt i live):

| Regel | Default | Betydning |
|---|---|---|
| `MIN_NET_EDGE` | 0.08 | Netto edge etter spread + fee + 5 pp modell-hårkutt |
| `MAX_POSITION_PCT` | 0.06 | Maks 6 % av bankroll i kostnad per marked |
| `MAX_CATEGORY_PCT` | 0.30 | Maks 30 % i samme kategori |
| `MAX_OPEN_POSITIONS` | 10 | Tak på åpne posisjoner |
| `DAILY_LOSS_HALT_PCT` | 0.04 | Ingen nye kjøp etter −4 % på 24 t |
| `WEEKLY_LOSS_HALT_PCT` | 0.10 | Ingen nye kjøp etter −10 % på 7 dager |
| `KELLY_FRACTION` | 0.25 | Kvart-Kelly, deretter cap 6 % |
| `MAX_SPREAD` | 0.06 | Hopper over vide bøker |
| `MIN_LIQUIDITY_USD` | 5000 | Kun likvide markeder |
| Confidence `low` | hard | Handles aldri, uansett edge |

Netto edge:

```
|p_hat − mid| − ½spread − forventet_fee − 0.05  ≥  0.08
```

---

## 12. Vanlige feil

| Symptom | Fix |
|---|---|
| `python` ikke gjenkjent | Python ikke i PATH. Reinstaller med «Add to PATH», åpne ny PowerShell |
| `Activate.ps1` blokkert | Ikke aktiver. Bruk `.\.venv\Scripts\python.exe` i stedet |
| `XAI_API_KEY mangler` | `.env` ikke i prosjektroten, eller nøkkelen har anførselstegn |
| `POLYMARKET_PRIVATE_KEY mangler` | Samme. Ingen mellomrom, ingen hermetegn |
| `401` / auth-feil ved bootstrap | Feil `SIGNATURE_TYPE` eller `FUNDER`. EOA = 0 og funder = samme adresse |
| Ingen PAPER BUY på timer | Normalt. 8 % netto er strengt. Sjekk `data/desk.db` → tabellen `decisions` |
| Live ordre avvist `allowance` | Gjør én manuell $1-handel i Polymarket-UI med samme wallet |
| Live ordre avvist `balance` | USDC/pUSD ligger på annen adresse enn `POLYMARKET_FUNDER` |
| Grok-timeout | Nettverk. Syklusen prøver igjen om 15 min |

---

## 13. Sikkerhet — les dette

- `.env` skal **aldri** lastes opp til GitHub, Discord eller chat
- Hvis private key lekker: flytt pengene ut med en gang og dropp wallet
- Live autonom handel kan tape **hele** beløpet i hot-wallet. Derfor begrenset innskudd
- Dette er ikke finansiell rådgivning. Prediction markets er spekulasjon

Etter 2–4 uker: se `decisions` og `fills` i `data/desk.db`. Hvis Grok ikke slår mid etter resolusjon, hev `MODEL_HAIRCUT` eller senk `ESTIMATE_BATCH`. Ikke gi den mer kapital.
