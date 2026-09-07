# Autonom Polymarket-desk

To roller i én prosess:

1. **Scout + Brain (Grok)** — henter likvide markeder, estimerer P(YES), sier nei når den ikke vet.
2. **Risk + Executor** — slår hardt på 8 % *netto* edge, 6 % posisjonstak, kategori-tak, tap-stopp, og plasserer limit-kjøp.

Grok-chatten kan **ikke** holde nøkler og signere CLOB-ordrer. Dette programmet er det autonome oppsettet. Du gir det en **egen hot-wallet** med et beløp du tåler å tape.

**Komplett PC-guide (start her): [SETUP.md](SETUP.md)**

## Regler som er kodet inn

- Inngang kun hvis `edge_net >= 8 %` etter spread + forventet fee + 5 pp modell-hårkutt
- Confidence `low` handles aldri
- Maks **6 %** av bankroll i kostnad per marked / samme event
- Maks **30 %** per kategori
- Maks 10 åpne posisjoner
- Daglig tap 4 % / ukentlig 10 % → ingen nye kjøp
- Hopper over 5/15-min crypto up/down
- Limit-kjøp, ikke markedsgaloppering
- `HALT`-fil i prosjektmappen stopper alt

## Hurtigstart

```bash
python3 -m venv .venv
source .venv/bin/activate          # Windows: .\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
cp .env.example .env               # Windows: copy .env.example .env
# fyll .env, deretter:
python -m agent.bootstrap_creds
python main.py once                # én paper-syklus
python main.py run                 # hvert 15. minutt
```

Live: sett `DRY_RUN=false` i `.env` etter at paper ser sunt ut.

Stopp øyeblikkelig: `touch HALT` (Windows: `New-Item HALT`).

## Sikkerhet

- Bruk **kun** en slank hot-wallet. Aldri seed til hovedkonto.
- `.env` skal aldri committes.
- Hvis nøkkelen lekker, flytt USDC ut og dropp wallet.
- Live autonom handel kan tape hele beløpet i wallet.
