# Autonom Polymarket-desk

Paper desk. `DRY_RUN` defaults to true. `DRY_RUN=false` locks CLOB and does not trade.

Buys only:

- **complement** — both legs (YES+NO on the same condition)
- **kalshi** — only if `|gap| − spread >= 0.10`
- **partition** — only if the set is complete

maker off. locked off. Grok off in paper.

Grok-chatten kan **ikke** holde nøkler og signere CLOB-ordrer. Dette programmet er det autonome oppsettet. Du gir det en **egen hot-wallet** med et beløp du tåler å tape.

**Komplett PC-guide (start her): [SETUP.md](SETUP.md)**

## Freeze (kodet inn)

- Kjøp kun complement (begge ben), kalshi kun hvis `|gap|−spread >= 0.10`, partition kun hvis settet er komplett
- maker off, locked off
- Grok off i paper (ingen xAI-kall når `DRY_RUN=true`)
- `DRY_RUN` default true. `DRY_RUN=false` låser CLOB og handler ikke
- Paper: nye kjøp stopper når equity er ned `>= DAILY_LOSS_HALT_PCT` fra `day_anchor`, eller `>= WEEKLY_LOSS_HALT_PCT` fra 7d-anker. Exits, redeem og paper_redeem kjører fortsatt
- `HALT`-fil hopper nye kjøp og live-salg; dashboard `:8788` kjører

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

Stopp øyeblikkelig: `touch HALT` (Windows: `New-Item HALT`).

## Sikkerhet

- Bruk **kun** en slank hot-wallet. Aldri seed til hovedkonto.
- `.env` skal aldri committes.
- Hvis nøkkelen lekker, flytt USDC ut og dropp wallet.
