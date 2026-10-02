"""
MFSI / SISO – Multi-Factor Sentinel Index
Script di aggiornamento automatico – v2.2

Scarica i dati di mercato e genera data.json letto dal widget HTML.
Il formato di data.json è retrocompatibile (score, date, factors):
i campi aggiuntivi "raw" e "status" servono solo per verifica e debug.

Novità v2.2: spread BTP-Bund giornaliero (Stooq) con componente di velocità;
la BCE mensile resta solo come ripiego.

Novità v2.1: momentum su scala continua (eliminato il salto all'incrocio
della media a 50 giorni).

Novità v2.0 rispetto alla v1:
  - VIX: mappatura per regimi sul livello assoluto (non più min-max su 12 mesi)
  - DXY: combinazione di livello (percentile 5 anni) e velocità (variazione 3 mesi)
  - Spread BTP-Bund: dato reale da API BCE (prima era un valore fisso a 120 bp)
  - Oro: scala continua invece di 5 gradini
  - Controlli su dati mancanti o non aggiornati, con esito tracciato in data.json

Dipendenze: pip install yfinance
Esecuzione:  python mfsi_updater.py
"""

import csv
import io
import json
import urllib.request
from datetime import datetime, timezone

import numpy as np

try:
    import yfinance as yf
except ImportError:
    print("Installa yfinance: pip install yfinance")
    raise

# ── CONFIGURAZIONE ──────────────────────────────────────────────
OUTPUT_FILE = "data.json"
MSCI_PROXY  = "^GSPC"          # S&P 500 come proxy dell'azionario globale

PESI = {"vix": 0.40, "spread": 0.15, "dxy": 0.15, "gold": 0.10, "mom": 0.20}

# Spread BTP-Bund: se impostato (es. 118), ha la precedenza sulle fonti automatiche.
# Lasciare None per usare il dato giornaliero automatico.
SPREAD_BP_MANUALE = None

MAX_GIORNI_DATO = 7            # oltre questa età un dato è considerato non aggiornato
# ────────────────────────────────────────────────────────────────

stato = {}   # esito per fattore: "ok" oppure motivo del valore di ripiego
raw   = {}   # valori grezzi di mercato, pubblicati per verifica


def interp(x, xs, ys):
    """Interpolazione lineare a tratti, con saturazione agli estremi."""
    return float(np.interp(x, xs, ys))


def serie_valida(s, nome):
    """Restituisce la serie pulita, oppure None se vuota o non aggiornata."""
    s = s.dropna()
    if len(s) < 2:
        stato[nome] = "dati mancanti"
        return None
    ultimo = s.index[-1]
    if hasattr(ultimo, "to_pydatetime"):
        ultimo = ultimo.to_pydatetime()
    if ultimo.tzinfo is None:
        ultimo = ultimo.replace(tzinfo=timezone.utc)
    eta = (datetime.now(timezone.utc) - ultimo).days
    if eta > MAX_GIORNI_DATO:
        stato[nome] = f"dato fermo da {eta} giorni"
        return None
    return s


def scarica_dati():
    """5 anni di storico: servono per percentile DXY e media mobile a 200 giorni."""
    tickers = ["^VIX", MSCI_PROXY, "DX-Y.NYB", "GC=F"]
    print("Scaricamento dati da Yahoo Finance...")
    data = yf.download(tickers, period="5y", auto_adjust=True, progress=False)["Close"]
    data.dropna(how="all", inplace=True)
    print(f"  Scaricati {len(data)} giorni di dati.")
    return data


# ── FATTORI ─────────────────────────────────────────────────────

def score_vix(data):
    """
    VIX (peso 40%) – mappatura per regimi sul livello assoluto.

    Il VIX ha livelli con significato proprio (media di lungo periodo ~19),
    quindi si usa il valore assoluto e non il min-max su 12 mesi, che rendeva
    "minimo" qualunque VIX tranquillo dopo un anno con un picco di volatilità.

      < 12   compiacenza eccessiva          -> 40-55  (prudenza)
      12-20  mercato sereno                 -> 55-70  (contesto favorevole)
      20-28  tensione, trend incerto        -> 55-65  (neutro)
      > 28   panico, logica contrarian      -> 65-100 (opportunità)
    """
    s = serie_valida(data["^VIX"], "vix")
    if s is None:
        return 50.0
    v = float(s.iloc[-1])
    raw["vix"] = round(v, 2)
    stato["vix"] = "ok"
    return round(interp(v,
                        [9,  12, 15, 18, 20, 24, 28, 35, 45],
                        [40, 55, 68, 70, 65, 55, 65, 85, 100]), 1)


def score_dxy(data):
    """
    Dollar Index (peso 15%) – livello e velocità.

    Livello: percentile del valore attuale sugli ultimi 5 anni (invertito).
    Velocità: variazione % a 3 mesi; un rafforzamento rapido penalizza.
    I due componenti pesano 50% ciascuno, coerentemente con il tooltip
    ("quando il dollaro si rafforza troppo e velocemente").
    """
    s = serie_valida(data["DX-Y.NYB"], "dxy")
    if s is None or len(s) < 70:
        stato.setdefault("dxy", "storico insufficiente")
        return 50.0
    v = float(s.iloc[-1])
    percentile = float((s < v).mean() * 100)
    var_3m = float(v / s.iloc[-63] - 1) * 100
    s_livello  = 100 - percentile
    s_velocita = interp(var_3m, [-6, -3, 0, 3, 6], [90, 70, 55, 30, 10])
    raw["dxy"] = round(v, 2)
    raw["dxy_var_3m_pct"] = round(var_3m, 2)
    stato["dxy"] = "ok"
    return round(0.5 * s_livello + 0.5 * s_velocita, 1)


def _csv(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (MFSI updater)"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return list(csv.DictReader(io.StringIO(r.read().decode("utf-8"))))


def spread_giornaliero():
    """
    Spread giornaliero da Stooq (rendimenti decennali IT e DE, chiusura di ogni giorno).
    Restituisce (spread_attuale_bp, variazione_1_mese_bp, data_ultimo_dato).
    """
    serie = {}
    for paese, ticker in (("IT", "10ity.b"), ("DE", "10dey.b")):
        righe = _csv(f"https://stooq.com/q/d/l/?s={ticker}&i=d")
        serie[paese] = {r["Date"]: float(r["Close"]) for r in righe if r.get("Close")}
    date_comuni = sorted(set(serie["IT"]) & set(serie["DE"]))
    if len(date_comuni) < 25:
        raise ValueError("storico spread insufficiente")
    ultima = date_comuni[-1]
    eta = (datetime.now(timezone.utc).date() - datetime.strptime(ultima, "%Y-%m-%d").date()).days
    if eta > 5:
        raise ValueError(f"dato spread fermo da {eta} giorni")
    sp = [(serie["IT"][d] - serie["DE"][d]) * 100 for d in date_comuni]
    return sp[-1], sp[-1] - sp[-22], ultima


def rendimento_bce(paese):
    """Ripiego: ultimo rendimento decennale MENSILE dalla BCE (circa un mese di ritardo)."""
    righe = _csv(f"https://data-api.ecb.europa.eu/service/data/IRS/"
                 f"M.{paese}.L.L40.CI.0000.EUR.N.Z?lastNObservations=1&format=csvdata")
    return float(righe[-1]["OBS_VALUE"]), righe[-1]["TIME_PERIOD"]


def score_spread():
    """
    Spread BTP-Bund (peso 15%) – livello e velocità.

    Livello: lo spread assoluto (80-100 bp minimi storici recenti, oltre 180 bp tensione seria).
    Velocità: variazione nell'ultimo mese. Un allargamento rapido (+20/30 bp) è il segnale
    di stress più importante, anche se il livello assoluto resta moderato.
    Pesi: 60% livello, 40% velocità.

    Fonte principale: Stooq, dato giornaliero.
    Ripiego: BCE, media mensile (solo livello, segnalato in data.json come non aggiornato).
    """
    var_1m = None
    if SPREAD_BP_MANUALE is not None:
        bp, fonte = float(SPREAD_BP_MANUALE), "valore manuale"
    else:
        try:
            bp, var_1m, giorno = spread_giornaliero()
            fonte = f"Stooq, chiusura {giorno}"
        except Exception as e:
            errore = f"{type(e).__name__}: {e}"[:80]
            try:
                it, mese = rendimento_bce("IT")
                de, _    = rendimento_bce("DE")
                bp, fonte = (it - de) * 100, f"BCE media mensile {mese} (giornaliero non disponibile: {errore})"
            except Exception as e2:
                stato["spread"] = f"nessuna fonte disponibile ({type(e2).__name__})"
                return 50.0

    s_livello = interp(bp, [80, 100, 130, 180, 250, 400], [90, 75, 55, 35, 15, 0])
    raw["spread_bp"] = round(bp, 1)
    raw["spread_fonte"] = fonte
    if var_1m is None:
        stato["spread"] = "ok" if fonte == "valore manuale" else "solo dato mensile"
        return round(s_livello, 1)

    s_velocita = interp(var_1m, [-20, 0, 15, 30], [80, 60, 35, 15])
    raw["spread_var_1m_bp"] = round(var_1m, 1)
    stato["spread"] = "ok"
    return round(0.6 * s_livello + 0.4 * s_velocita, 1)


def score_gold(data):
    """
    Oro (peso 10%) – divergenza oro/azionario sulle ultime 4 settimane.
    Oro che sale mentre la borsa scende = fuga verso la sicurezza = score basso.
    """
    gold = serie_valida(data["GC=F"], "gold")
    mkt  = serie_valida(data[MSCI_PROXY], "gold")
    if gold is None or mkt is None or len(gold) < 21 or len(mkt) < 21:
        stato.setdefault("gold", "dati mancanti")
        return 50.0
    ret_gold = float(gold.iloc[-1] / gold.iloc[-21] - 1) * 100
    ret_mkt  = float(mkt.iloc[-1]  / mkt.iloc[-21]  - 1) * 100
    div = ret_gold - ret_mkt
    raw["oro_vs_borsa_4sett_pct"] = round(div, 2)
    stato["gold"] = "ok"
    return round(interp(div, [-5, -2, 0, 2, 5], [85, 70, 55, 38, 20]), 1)


def score_momentum(data):
    """
    Momentum (peso 20%) – distanza dell'indice dalle medie a 200 e 50 giorni.
    Scala continua: niente salti quando il prezzo incrocia una media
    (nella v1/v2.0 l'incrocio della media a 50 giorni spostava il fattore
    di circa 24 punti in un colpo, e lo score totale di circa 5).
    Trend di fondo (SMA 200) pesa 70%, trend di breve (SMA 50) pesa 30%.
    """
    mkt = serie_valida(data[MSCI_PROXY], "mom")
    if mkt is None or len(mkt) < 200:
        stato.setdefault("mom", "storico insufficiente")
        return 50.0
    v       = float(mkt.iloc[-1])
    sma_50  = float(mkt.rolling(50).mean().iloc[-1])
    sma_200 = float(mkt.rolling(200).mean().iloc[-1])
    dist_200 = (v - sma_200) / sma_200 * 100
    dist_50  = (v - sma_50)  / sma_50  * 100

    s200 = interp(dist_200, [-10, -5, 0, 5, 10], [10, 25, 45, 70, 85])
    s50  = interp(dist_50,  [-5, -2, 0, 2, 5],   [20, 35, 50, 65, 80])

    raw["borsa_dist_sma200_pct"] = round(dist_200, 2)
    raw["borsa_dist_sma50_pct"]  = round(dist_50, 2)
    stato["mom"] = "ok"
    return round(0.7 * s200 + 0.3 * s50, 1)


# ── OUTPUT ──────────────────────────────────────────────────────

def calcola_score(f):
    return round(sum(f[k] * PESI[k] for k in PESI), 1)


def genera_json(score, factors):
    now = datetime.now(timezone.utc)
    out = {
        "score": score,
        "date": now.strftime("Aggiornato il %d/%m/%Y"),
        "timestamp": now.isoformat(),
        "factors": factors,
        "raw": raw,
        "status": stato,
    }
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=2, ensure_ascii=False)
    print(f"\nScritto {OUTPUT_FILE}:")
    print(json.dumps(out, indent=2, ensure_ascii=False))


def main():
    print("=" * 50)
    print(" MFSI / SISO – Updater v2.2")
    print("=" * 50)

    try:
        data = scarica_dati()
    except Exception as e:
        print(f"Errore nel download: {e} – data.json non modificato.")
        return

    factors = {
        "vix":    score_vix(data),
        "spread": score_spread(),
        "dxy":    score_dxy(data),
        "gold":   score_gold(data),
        "mom":    score_momentum(data),
    }

    # Se più di due fattori sono di ripiego, meglio non pubblicare un segnale falsato
    ripieghi = [k for k, v in stato.items() if v != "ok"]
    if len(ripieghi) > 2:
        print(f"Troppi fattori senza dati validi ({ripieghi}) – data.json non modificato.")
        return

    score = calcola_score(factors)

    print("\nFattori:")
    for k in PESI:
        print(f"  {k:<7} ({int(PESI[k]*100)}%): {factors[k]:>5}   [{stato.get(k)}]")
    print(f"\n  SCORE FINALE: {score}/100")
    genera_json(score, factors)


if __name__ == "__main__":
    main()
