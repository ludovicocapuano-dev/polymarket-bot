# Known Trade Patterns

## Citta' problematiche
- **Toronto**: WU diverge da OpenMeteo fino a 12°C. WR weather basso. Richiedere 3+ fonti.
- **Phoenix**: temperature estreme meno prevedibili. Edge reale < edge stimato su same-day.
- **Chicago**: vento influenza "feels like" vs actual temp. Verificare se il mercato usa "high" o "feels like".

## Orizzonte e WR storico
- **Same-day (0d)**: WR ~80% — forecast accurato, edge reale vicino a stimato
- **Next-day (+1d)**: WR ~65% — serve edge >= 0.08 per break-even su BUY_NO
- **+2 giorni**: WR ~55% — troppo incerto, solo con edge >= 0.12

## Prezzo entry e payoff
- BUY_NO @ $0.80+: payoff 0.25:1, richiede WR > 80%
- BUY_NO @ $0.70-0.80: payoff 0.43:1, richiede WR > 70%
- BUY_NO @ $0.60-0.70: payoff 0.67:1, richiede WR > 60%
- BUY_YES @ $0.10-0.15: payoff 6:1+, WR > 15% basta

## Pattern di perdita ricorrenti
1. **Single-source high-price**: 1 sola fonte + prezzo > $0.65 = 75% loss rate
2. **High uncertainty next-day**: forecast sigma > 4°C + orizzonte +1d = 70% loss rate
3. **Edge basso next-day**: edge < 0.08 su +1d = 65% loss rate
4. **Mercato illiquido**: spread > 5% + depth < $100 = slippage erode edge

## Outlier positivi
- **BUY_YES low-price**: entry $0.10-0.15 con 3+ fonti concordanti = WR 40%+ con payoff 6:1
- **Same-day con shift**: forecast shift >= 1°C dopo GFS release = edge reale 2x stimato
