"""
Connettore OCF — Elenchi iscritti ufficiali (fonte massiva, senza captcha).

A differenza della "Ricerca nelle sezioni dell'Albo" (che è protetta da CAPTCHA e
quindi consultabile solo un nominativo alla volta a mano), OCF pubblica in chiaro
gli ELENCHI ISCRITTI completi come archivio ZIP di CSV per regione:

    https://www.organismocf.it/portal/web/portale-ocf/elenchi-iscritti

    CF-ABILITATI.zip  → consulenti finanziari abilitati all'offerta fuori sede
                        (quelli con mandato da una rete: il nostro target)
    CF-AUTONOMI.zip   → consulenti finanziari autonomi (fee-only, indipendenti)
    SOCIETA-CF.zip    → società di consulenza finanziaria

Ogni CSV ha 12 colonne, senza riga di intestazione (l'intestazione sta in un file
`_HEADER_*.txt` a parte):

    NOME, COGNOME, DATA_NASCITA, LUOGO_NASCITA, SIGLA_PROVINCIA_NASCITA,
    INDIRIZZO, CIVICO, CAP, COMUNE, PROVINCIA,
    DENOMINAZIONE_SOCIETA_CONSULENZA, REGIONE

⚠️ MINIMIZZAZIONE DEI DATI (GDPR art. 5.1.c) — scelta deliberata:
l'elenco contiene l'INDIRIZZO DI RESIDENZA e la DATA DI NASCITA completa. Per il
recruiting non servono: teniamo comune/provincia/regione (dove lavora), l'ANNO di
nascita (seniority) e un hash irreversibile di nome+cognome+data per avere
un'identità stabile tra un elenco e il successivo. Indirizzo, civico, CAP, luogo
di nascita e data esatta NON vengono mai scritti in database.
"""

import csv
import hashlib
import io
import logging
import re
import zipfile
from datetime import date

import requests

logger = logging.getLogger(__name__)

FONTE = "ocf_elenco"

OCF_BASE = "https://www.organismocf.it"
PAGINA_ELENCHI = f"{OCF_BASE}/portal/web/portale-ocf/elenchi-iscritti"

# NB: senza "www." il portale risponde 500; senza User-Agent da browser redirige
# alla pagina di login. Entrambe le cose sono state verificate sul campo.
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/128.0 Safari/537.36")

TIMEOUT = 120

# Elenchi disponibili → nome del file dentro la cartella pubblicata da OCF
ELENCHI = {
    "abilitati": "CF-ABILITATI.zip",   # con mandato di una rete → target recruiting
    "autonomi":  "CF-AUTONOMI.zip",    # fee-only indipendenti
    "societa":   "SOCIETA-CF.zip",
}

# Colonne del CSV (l'ordine è quello di _HEADER_CF_ABILITATI.txt)
COL_NOME, COL_COGNOME, COL_DATA_NASCITA = 0, 1, 2
COL_COMUNE, COL_PROVINCIA = 8, 9
COL_SOCIETA, COL_REGIONE = 10, 11


# ── Normalizzazione delle reti ────────────────────────────────────────────────
# Le denominazioni ufficiali sono ragioni sociali lunghe ("FIDEURAM - INTESA
# SANPAOLO PRIVATE BANKING SPA IN FORMA ABBREVIATA..."): le riduciamo a etichette
# brevi e stabili, così ricerca e statistiche non si spaccano su una virgola.
_RETI_NOTE = [
    ("FIDEURAM",                     "Fideuram"),
    ("INTESA SANPAOLO PRIVATE",      "Intesa Sanpaolo Private Banking"),
    ("INTESA SANPAOLO",              "Intesa Sanpaolo"),
    ("MEDIOLANUM",                   "Banca Mediolanum"),
    ("FINECO",                       "FinecoBank"),
    ("ALLIANZ BANK",                 "Allianz Bank"),
    ("BANCA GENERALI",               "Banca Generali"),
    ("AZIMUT",                       "Azimut"),
    ("MEDIOBANCA PREMIER",           "Mediobanca Premier"),
    ("ZURICH",                       "Zurich Bank"),
    ("WIDIBA",                       "Banca Widiba"),
    ("SANPAOLO INVEST",              "Sanpaolo Invest"),
    ("BANCA NAZIONALE DEL LAVORO",   "BNL BNP Paribas"),
    ("BNL",                          "BNL BNP Paribas"),
    ("CREDEM",                       "Credem Euromobiliare"),
    ("BANCA PATRIMONI SELLA",        "Banca Patrimoni Sella"),
    ("IW PRIVATE",                   "IW Private Investments"),
    ("UNICREDIT",                    "UniCredit"),
    ("BANCA INVESTIS",               "Banca Investis"),
    ("BPER",                         "BPER Banca"),
    ("MONTE DEI PASCHI",             "Banca MPS"),
    ("DEUTSCHE BANK",                "Deutsche Bank"),
]

_SUFFISSI_LEGALI = re.compile(
    r"\b(S\.?P\.?A\.?|S\.?R\.?L\.?|SGR|SIM|S\.?C\.?P\.?A\.?|IN FORMA ABBREVIATA.*|"
    r"SOCIETA'? PER AZIONI|BANCA POPOLARE|GRUPPO)\b", re.IGNORECASE)


def normalizza_rete(denominazione: str) -> str:
    """Ragione sociale ufficiale → etichetta breve e stabile della rete."""
    d = (denominazione or "").strip().strip(";").strip()
    if not d:
        return ""
    up = d.upper()
    for chiave, etichetta in _RETI_NOTE:
        if chiave in up:
            return etichetta
    # Fallback: togli forme societarie e punteggiatura, poi Title Case
    pulita = _SUFFISSI_LEGALI.sub("", d)
    pulita = re.sub(r"[.;,]+", " ", pulita)
    pulita = re.sub(r"\s+", " ", pulita).strip()
    return pulita.title() if pulita else d.title()


def chiave_persona(nome: str, cognome: str, data_nascita: str) -> str:
    """
    Identità stabile fra due elenchi successivi, senza conservare la data di
    nascita: sha256 di nome|cognome|data. Irreversibile, ma confrontabile.
    """
    grezzo = f"{(nome or '').strip().upper()}|{(cognome or '').strip().upper()}|{(data_nascita or '').strip()}"
    return hashlib.sha256(grezzo.encode("utf-8")).hexdigest()[:32]


def _anno(data_nascita: str):
    """'18/01/1977' → 1977. None se non interpretabile."""
    m = re.search(r"(19|20)\d{2}", data_nascita or "")
    return int(m.group(0)) if m else None


def scarica_zip(elenco: str = "abilitati", sessione=None) -> bytes:
    """
    Scarica l'archivio ZIP dell'elenco richiesto.
    Solleva requests.HTTPError se il portale non risponde 200.
    """
    nome_file = ELENCHI.get(elenco)
    if not nome_file:
        raise ValueError(f"Elenco sconosciuto: {elenco}. Attesi: {', '.join(ELENCHI)}")

    s = sessione or requests.Session()
    s.headers.setdefault("User-Agent", UA)

    # Il download passa da una resource URL del portlet Liferay: il percorso del
    # file sul filesystem OCF è un parametro. Lo componiamo qui invece di
    # scrapare la pagina ogni volta, ma se cambia basta rileggere PAGINA_ELENCHI.
    url = (
        f"{PAGINA_ELENCHI}?p_p_id=ElencoIscritti_INSTANCE_FTVQlWrV3M4a"
        f"&p_p_lifecycle=2&p_p_state=normal&p_p_mode=view"
        f"&p_p_cacheability=cacheLevelPage"
        f"&_ElencoIscritti_INSTANCE_FTVQlWrV3M4a_filePath="
        f"%2Fdati%2Fnfs%2Foutput%2Focf-esb%2FelenchiIscritti%2F{nome_file}"
    )
    r = s.get(url, headers={"Referer": PAGINA_ELENCHI}, timeout=TIMEOUT)
    r.raise_for_status()
    if not r.content[:2] == b"PK":
        raise ValueError("La risposta OCF non è un archivio ZIP (portale cambiato?)")
    logger.info("OCF: scaricato %s (%d KB)", nome_file, len(r.content) // 1024)
    return r.content


def data_elenco(zip_bytes: bytes):
    """
    Data di aggiornamento dichiarata da OCF nel file `_disclaimer.txt`
    ("elenchi aggiornati al 03 settembre 2026"). Fallback: data odierna.
    """
    mesi = {"gennaio": 1, "febbraio": 2, "marzo": 3, "aprile": 4, "maggio": 5,
            "giugno": 6, "luglio": 7, "agosto": 8, "settembre": 9,
            "ottobre": 10, "novembre": 11, "dicembre": 12}
    try:
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as z:
            for nome in z.namelist():
                if "disclaimer" in nome.lower():
                    testo = z.read(nome).decode("utf-8", errors="replace").lower()
                    m = re.search(r"(\d{1,2})\s+([a-zà]+)\s+(\d{4})", testo)
                    if m and m.group(2) in mesi:
                        return date(int(m.group(3)), mesi[m.group(2)], int(m.group(1)))
    except Exception as e:  # pragma: no cover — diagnostica, non deve mai bloccare
        logger.warning("OCF: data elenco non leggibile (%s)", e)
    return date.today()


def leggi_iscritti(zip_bytes: bytes):
    """
    Generatore di dict normalizzati e MINIMIZZATI, uno per consulente.
    Salta righe malformate senza mai sollevare: un CSV storto non deve far
    fallire una sincronizzazione da 56.000 righe.

    Campi restituiti: chiave, nome, cognome, anno_nascita, comune, provincia,
    regione, rete, rete_raw.
    """
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as z:
        for nome_file in z.namelist():
            if not nome_file.upper().endswith(".CSV"):
                continue
            testo = z.read(nome_file).decode("utf-8", errors="replace")
            for riga in csv.reader(io.StringIO(testo)):
                if len(riga) < 12:
                    continue
                nome = (riga[COL_NOME] or "").strip()
                cognome = (riga[COL_COGNOME] or "").strip()
                if not cognome:
                    continue
                rete_raw = (riga[COL_SOCIETA] or "").strip().strip(";").strip()
                yield {
                    "chiave": chiave_persona(nome, cognome, riga[COL_DATA_NASCITA]),
                    "nome": nome.title(),
                    "cognome": cognome.title(),
                    "anno_nascita": _anno(riga[COL_DATA_NASCITA]),
                    "comune": (riga[COL_COMUNE] or "").strip().title(),
                    "provincia": (riga[COL_PROVINCIA] or "").strip().upper()[:2],
                    "regione": (riga[COL_REGIONE] or "").strip().title(),
                    "rete": normalizza_rete(rete_raw),
                    "rete_raw": rete_raw[:200],
                }
