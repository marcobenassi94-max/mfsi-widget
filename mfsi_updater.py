"""
MFSI – Multi-Factor Sentinel Index
Script di aggiornamento automatico giornaliero

Scarica i dati di mercato da Yahoo Finance e genera data.json
che il widget HTML legge per aggiornarsi.

Dipendenze: pip install yfinance
Esecuzione:  python mfsi_updater.py
Automazione: vedi istruzioni in fondo al file
"""

import json
import os
from datetime import datetime, timezone

try:
    import yfinance as yf
except ImportError:
    print("Installa yfinance: pip install yfinance")
    raise

# ── CONFIGURAZIONE ──────────────────────────────────────────────
OUTPUT_FILE = "data.json"   # file letto dal widget HTML
MSCI_PROXY  = "^GSPC"      # S&P 500 come proxy MSCI World
# ────────────────────────────────────────────────────────────────

def scarica_dati():
    """Scarica 1 anno di dati per tutti i ticker necessari."""
    tickers = ["^VIX", "^GSPC", "DX-Y.NYB", "GC=F"]
    print("Scaricamento dati da Yahoo Finance...")
    data = yf.download(tickers, period="1y", auto_adjust=True, progress=False)["Close"]
    data.dropna(how="all", inplace=True)
    print(f"  Scaricati {len(data)} giorni di dati.")
    return data

def score_vix(data):
    """
    VIX SCORE (peso 40%)
    VIX alto = paura = potenziale opportunità di acquisto.
    Score alto = VIX alto rispetto ai suoi percentili storici.
    """
    vix = data["^VIX"].dropna()
    if len(vix) < 2:
        return 50.0

    v_last = float(vix.iloc[-1])
    v_min  = float(vix.min())
    v_max  = float(vix.max())

    # Percentile normalizzato 0-100
    raw = (v_last - v_min) / (v_max - v_min) * 100 if v_max != v_min else 50.0

    # Un VIX a 35+ segnala panico → score molto alto (Compra)
    # Un VIX a 12-15 segnala euforia → score basso (Cautela)
    return round(min(100, max(0, raw)), 1)

def score_spread():
    """
    SPREAD BTP-BUND SCORE (peso 15%)
    Spread basso = Europa stabile = score alto (positivo).
    
    Yahoo Finance non fornisce dati di spread direttamente,
    usiamo un valore manuale aggiornabile o una stima statica.
    NOTA: Per aggiornamento automatico reale, puoi usare le API
    di Investing.com o BundesBank (free, vedi commento sotto).
    """
    # Valore approssimativo attuale dello spread BTP-Bund in bp.
    # Modifica questo valore manualmente se non hai un'API live.
    # Oppure integra: https://api.bundesbank.de/service/data/BBDP1
    spread_bp_stimato = 120  # esempio: 120 punti base

    # Score inverso: spread alto = rischio = score basso
    if spread_bp_stimato < 100:
        return 90.0
    elif spread_bp_stimato < 150:
        return 75.0
    elif spread_bp_stimato < 200:
        return 55.0
    elif spread_bp_stimato < 300:
        return 30.0
    else:
        return 10.0

def score_dxy(data):
    """
    DOLLAR INDEX SCORE (peso 15%)
    DXY forte = stress = score basso.
    Score = inverso del percentile del DXY su 1 anno.
    """
    dxy = data["DX-Y.NYB"].dropna()
    if len(dxy) < 2:
        return 50.0

    v_last = float(dxy.iloc[-1])
    v_min  = float(dxy.min())
    v_max  = float(dxy.max())

    if v_max == v_min:
        return 50.0

    # Più il DXY è alto rispetto al range, più il score scende
    pct = (v_last - v_min) / (v_max - v_min) * 100
    return round(min(100, max(0, 100 - pct)), 1)

def score_gold(data):
    """
    ORO SCORE (peso 10%)
    Oro in salita mentre azionario scende → conferma ribasso → score basso.
    Oro stabile o in calo → nessuna fuga verso safe haven → score alto.
    """
    gold = data["GC=F"].dropna()
    mkt  = data["^GSPC"].dropna()

    if len(gold) < 20 or len(mkt) < 20:
        return 50.0

    # Divergenza: rendimento oro vs mercato negli ultimi 20 giorni
    ret_gold = float(gold.iloc[-1] / gold.iloc[-20] - 1) * 100
    ret_mkt  = float(mkt.iloc[-1]  / mkt.iloc[-20]  - 1) * 100

    divergenza = ret_gold - ret_mkt  # positivo = oro sale, mercato scende

    if divergenza > 5:
        return 20.0   # forte segnale di rifugio → ribasso confermato
    elif divergenza > 2:
        return 38.0
    elif divergenza > 0:
        return 52.0
    elif divergenza > -2:
        return 65.0
    else:
        return 80.0   # oro scende, mercato sale → clima risk-on

def score_momentum(data):
    """
    MOMENTUM SCORE (peso 20%)
    Mercato sopra media 200 giorni = trend positivo = score alto.
    Mercato sotto media 50 giorni = trend debole = score basso.
    """
    mkt = data["^GSPC"].dropna()

    if len(mkt) < 200:
        return 50.0

    v_last   = float(mkt.iloc[-1])
    sma_50   = float(mkt.rolling(50).mean().iloc[-1])
    sma_200  = float(mkt.rolling(200).mean().iloc[-1])

    sopra_200 = v_last > sma_200
    sopra_50  = v_last > sma_50

    # Distanza percentuale dalla SMA 200 (normalizzata)
    dist_200 = (v_last - sma_200) / sma_200 * 100

    if sopra_200 and sopra_50:
        # Trend forte: più siamo sopra la 200, più il momentum è solido
        s = min(85, 60 + dist_200 * 1.5)
    elif sopra_200 and not sopra_50:
        # Sopra 200 ma sotto 50: fase di correzione nel trend rialzista
        s = 45.0
    elif not sopra_200 and v_last > sma_50:
        # Rimbalzo tecnico in bear market
        s = 30.0
    else:
        # Sotto entrambe: trend ribassista
        s = max(5, 20 + dist_200)

    return round(min(100, max(0, s)), 1)

def calcola_score(f):
    """Calcola il punteggio finale pesato (0-100)."""
    return round(
        f["vix"]    * 0.40 +
        f["spread"] * 0.15 +
        f["dxy"]    * 0.15 +
        f["gold"]   * 0.10 +
        f["mom"]    * 0.20,
        1
    )

def genera_json(score, factors):
    """Genera il file data.json letto dal widget HTML."""
    now = datetime.now(timezone.utc)
    data = {
        "score": score,
        "date": now.strftime("Aggiornato il %d/%m/%Y"),
        "timestamp": now.isoformat(),
        "factors": factors
    }
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    print(f"\nScritto {OUTPUT_FILE}:")
    print(json.dumps(data, indent=2, ensure_ascii=False))

def main():
    print("=" * 50)
    print(" MFSI – Multi-Factor Sentinel Index Updater")
    print("=" * 50)

    try:
        data = scarica_dati()
    except Exception as e:
        print(f"Errore nel download: {e}")
        return

    factors = {
        "vix":    score_vix(data),
        "spread": score_spread(),
        "dxy":    score_dxy(data),
        "gold":   score_gold(data),
        "mom":    score_momentum(data),
    }

    score = calcola_score(factors)

    print(f"\nFattori calcolati:")
    print(f"  VIX Score     (40%): {factors['vix']}")
    print(f"  Spread Score  (15%): {factors['spread']}")
    print(f"  DXY Score     (15%): {factors['dxy']}")
    print(f"  Gold Score    (10%): {factors['gold']}")
    print(f"  Momentum Score(20%): {factors['mom']}")
    print(f"\n  SCORE FINALE: {score}/100")

    if score >= 65:
        print("  SEGNALE: COMPRA / ACCUMULA")
    elif score >= 40:
        print("  SEGNALE: NEUTRO / ATTENDI")
    else:
        print("  SEGNALE: CAUTELA / RIDUCI")

    genera_json(score, factors)
    print("\nAggiornamento completato.")

if __name__ == "__main__":
    main()


# ================================================================
# ISTRUZIONI PER L'AUTOMAZIONE GIORNALIERA
# ================================================================
#
# OPZIONE A — GitHub Actions (GRATUITA, consigliata)
# ─────────────────────────────────────────────────
# 1. Crea un repository GitHub (es. "mfsi-widget")
# 2. Carica in esso: mfsi-widget.html, mfsi_updater.py, data.json
# 3. Crea il file .github/workflows/update.yml con:
#
#   name: MFSI Daily Update
#   on:
#     schedule:
#       - cron: '0 19 * * 1-5'   # ogni giorno feriale alle 19:00 UTC
#     workflow_dispatch:           # permette lancio manuale
#   jobs:
#     update:
#       runs-on: ubuntu-latest
#       steps:
#         - uses: actions/checkout@v4
#         - uses: actions/setup-python@v5
#           with: { python-version: '3.11' }
#         - run: pip install yfinance
#         - run: python mfsi_updater.py
#         - run: |
#             git config user.email "bot@mfsi"
#             git config user.name "MFSI Bot"
#             git add data.json
#             git commit -m "Update MFSI $(date +'%Y-%m-%d')" || echo "No changes"
#             git push
#
# 4. Abilita GitHub Pages per il repository (Settings → Pages)
#    e imposta DATA_URL nel widget su:
#    "https://TUO_UTENTE.github.io/mfsi-widget/data.json"
#
# OPZIONE B — Server Linux (cron)
# ────────────────────────────────
# Aggiungi al crontab (crontab -e):
#   0 19 * * 1-5 cd /var/www/html/mfsi && python3 mfsi_updater.py
#
# OPZIONE C — Windows Task Scheduler
# ────────────────────────────────────
# Crea un'attività pianificata che esegue ogni giorno:
#   python C:\percorso\mfsi_updater.py
# ================================================================
