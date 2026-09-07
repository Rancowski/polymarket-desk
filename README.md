# Autonom Polymarket-desk

To roller i én prosess:

1. **Scout + Brain (Grok)** — henter likvide markeder, estimerer P(YES), sier nei når den ikke vet.
2. **Risk + Executor** — slår hardt på 8 % *netto* edge, 6 % posisjonstak, kategori-tak, tap-stopp, og plasserer limit-kjøp.

Grok-chatten kan ikke holde nøkler og signere CLOB-ordrer. Dette programmet er det autonome oppsettet. Gi det en egen hot-wallet med et beløp du tåler å tape.

## Regler som er kodet inn

- Inngang kun hvis edge_net >= 8 % etter spread + forventet fee + 5 pp modell-hårkutt
- Confidence low handles aldri
- Maks 6 % av bankroll i kostnad per marked / samme event
- Maks 30 % per kategori
- Maks 10 åpne posisjoner
- Daglig tap 4 % / ukentlig 10 % = ingen nye kjøp
- Hopper over 5/15-min crypto up/down
- Limit-kjøp
- Filen HALT i prosjektmappen stopper alt

## Installasjon

```bash
git clone https://github.com/Rancowski/polymarket-desk.git
cd polymarket-desk
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Fyll .env med POLYMARKET_PRIVATE_KEY, POLYMARKET_FUNDER, XAI_API_KEY. Kjør `python -m agent.bootstrap_creds` og lim inn POLY_API_*.

```bash
python main.py once     # én syklus, paper når DRY_RUN=true
python main.py run      # hvert 15. minutt
python main.py status
touch HALT              # nødstopp
```

Sett DRY_RUN=false først når hot-wallet er funded og paper ser sunt ut.
