"""
Modulo 5 — Ricerca Automatica Candidati via Apify (LinkedIn Profile Search).
Cerca figure professionali su LinkedIn tramite l'actor harvestapi/linkedin-profile-search
e le importa nella pipeline.
"""

import io
import csv
import json
import logging
import os
import time
import threading
import uuid
import requests
from flask import Blueprint, render_template, request, jsonify, Response
from database import get_db
from ai_helpers import analizza_profilo_linkedin
from dedup import is_duplicate

log = logging.getLogger(__name__)

# Blueprint per il modulo ricerca
ricerca_bp = Blueprint("ricerca", __name__)

# Actor Apify per la ricerca persone su LinkedIn (no cookies richiesti)
APIFY_ACTOR = "harvestapi~linkedin-profile-search"
APIFY_BASE  = "https://api.apify.com/v2"

# Città per rotazione geografica — nomi esatti come li indicizza LinkedIn/Apify
# IMPORTANTE: usare "Rome, Latium, Italy" non "Roma" (che Apify confonde con Romania)
_CITTA_ROTAZIONE = {
    'A': [
        'Milan, Lombardy, Italy',
        'Rome, Latium, Italy',
        'Turin, Piedmont, Italy',
        'Bologna, Emilia-Romagna, Italy',
        'Florence, Tuscany, Italy',
        'Naples, Campania, Italy',
        'Genoa, Liguria, Italy',
        'Verona, Veneto, Italy',
    ],
    'B': [
        'Milan, Lombardy, Italy',
        'Rome, Latium, Italy',
        'Turin, Piedmont, Italy',
        'Bologna, Emilia-Romagna, Italy',
        'Florence, Tuscany, Italy',
        'Naples, Campania, Italy',
        'Padua, Veneto, Italy',
        'Venice, Veneto, Italy',
    ],
}

# Dizionario ruoli correlati: usato per espandere la ricerca quando il ruolo principale
# non produce profili nuovi dopo tutti i tentativi.
RUOLI_CORRELATI = {
    'consulente patrimoniale': [
        'private banker',
        'wealth manager',
        'consulente finanziario',
        'promotore finanziario',
        'financial advisor',
        'consulente investimenti',
        'gestore patrimoni',
        'banker',
    ],
    'private banker': [
        'consulente patrimoniale',
        'wealth manager',
        'consulente finanziario',
        'relationship manager',
        'promotore finanziario',
        'financial advisor',
        'banker',
    ],
    'wealth manager': [
        'private banker',
        'consulente patrimoniale',
        'consulente finanziario',
        'investment advisor',
        'asset manager',
        'financial planner',
        'promotore finanziario',
    ],
    'consulente finanziario': [
        'private banker',
        'consulente patrimoniale',
        'promotore finanziario',
        'financial advisor',
        'wealth manager',
        'consulente investimenti',
        'banker',
    ],
    'promotore finanziario': [
        'consulente finanziario',
        'private banker',
        'consulente patrimoniale',
        'financial advisor',
        'wealth manager',
    ],
    'banker': [
        'private banker',
        'consulente finanziario',
        'wealth manager',
        'consulente patrimoniale',
        'relationship manager',
    ],
}

# Keywords generiche per FASE 3 (fallback quando ruolo + correlati non bastano)
_KW_GENERICHE_FALLBACK = [
    'banca consulenza finanziaria',
    'private banking italia',
    'consulenza patrimoniale italia',
    'finanza personale',
]


def _leggi_aggiorna_offset(db, tipo_profilo: str, ruoli: list) -> tuple:
    """
    Legge l'offset corrente per questo tipo_profilo e lo incrementa.
    Restituisce (start_page, ruolo_corrente, citta_corrente).
    - offset_corrente: 0, 10, 20, ... 90, poi reset a 0
    - indice_ruolo: ruota tra i ruoli target uno alla volta
    - indice_citta: ruota tra le città di _CITTA_ROTAZIONE
    """
    citta_lista = _CITTA_ROTAZIONE.get(tipo_profilo, ['Italy'])

    row = db.execute(
        "SELECT * FROM search_offset WHERE tipo_profilo = ?", (tipo_profilo,)
    ).fetchone()

    if not row:
        db.execute(
            "INSERT INTO search_offset (tipo_profilo, offset_corrente, indice_ruolo, indice_citta) VALUES (?, 0, 0, 0)",
            (tipo_profilo,)
        )
        db.commit()
        offset_corrente = 0
        indice_ruolo    = 0
        indice_citta    = 0
    else:
        offset_corrente = row['offset_corrente']
        indice_ruolo    = row['indice_ruolo']
        indice_citta    = row['indice_citta']

    # Valori correnti da usare in questa ricerca
    start_page      = (offset_corrente // 10) + 1
    ruolo_corrente  = ruoli[indice_ruolo % len(ruoli)] if ruoli else ""
    citta_corrente  = citta_lista[indice_citta % len(citta_lista)]

    # Aggiorna per la prossima ricerca
    nuovo_offset       = (offset_corrente + 10) % 100
    nuovo_indice_ruolo = (indice_ruolo + 1) % max(len(ruoli), 1)
    nuovo_indice_citta = (indice_citta + 1) % len(citta_lista)

    db.execute(
        """UPDATE search_offset SET
           offset_corrente=?, indice_ruolo=?, indice_citta=?,
           ultimo_aggiornamento=CURRENT_TIMESTAMP
           WHERE tipo_profilo=?""",
        (nuovo_offset, nuovo_indice_ruolo, nuovo_indice_citta, tipo_profilo)
    )
    db.commit()

    return start_page, ruolo_corrente, citta_corrente


# Dizionario di normalizzazione città — Apify riconosce il formato completo "Città, Regione, Paese"
_CITTA_NORMALIZE = {
    'roma':     'Rome, Latium, Italy',
    'rome':     'Rome, Latium, Italy',
    'milano':   'Milan, Lombardy, Italy',
    'milan':    'Milan, Lombardy, Italy',
    'torino':   'Turin, Piedmont, Italy',
    'turin':    'Turin, Piedmont, Italy',
    'napoli':   'Naples, Campania, Italy',
    'naples':   'Naples, Campania, Italy',
    'firenze':  'Florence, Tuscany, Italy',
    'florence': 'Florence, Tuscany, Italy',
    'bologna':  'Bologna, Emilia-Romagna, Italy',
    'genova':   'Genoa, Liguria, Italy',
    'genoa':    'Genoa, Liguria, Italy',
    'palermo':  'Palermo, Sicily, Italy',
    'venezia':  'Venice, Veneto, Italy',
    'venice':   'Venice, Veneto, Italy',
    'verona':   'Verona, Veneto, Italy',
    'bari':     'Bari, Apulia, Italy',
}


def _normalizza_citta(citta: str) -> str:
    """
    Normalizza il nome città per Apify.
    - Se è nel dizionario → usa il nome completo
    - Se non contiene già 'italy' → aggiunge ', Italy'
    - Se vuota → restituisce 'Italy'
    """
    if not citta:
        return "Italy"
    key = citta.strip().lower()
    if key in _CITTA_NORMALIZE:
        return _CITTA_NORMALIZE[key]
    if "italy" not in key:
        return citta.strip() + ", Italy"
    return citta.strip()


def cerca_apify(ruolo, citta="", paese="", azienda="", parole_chiave="", num_pagine=1,
                ruoli_lista=None, forza_italia=True, progress_cb=None, start_page=1):
    """
    Flusso asincrono Apify in due step:
      STEP 1 — POST /acts/{actor}/runs  → avvia run, ottieni run_id
      STEP 2 — GET  /actor-runs/{id}    → poll ogni 5s finché SUCCEEDED
      STEP 3 — GET  /datasets/{id}/items → recupera risultati (max 10)
    progress_cb(pct, messaggio) viene chiamata ad ogni step se fornita.
    start_page: pagina di partenza (1=prima, 2=seconda, …) per variare i risultati.
    Restituisce (lista_profili, errore).
    """
    api_key = os.environ.get("APIFY_API_KEY", "")
    if not api_key:
        return None, "APIFY_API_KEY non configurata nel file .env"

    run_input = {
        "takePages": num_pagine,
        "startPage": max(1, start_page),   # offset: varia ad ogni ricerca
        "maxItems": 10,                    # rinominato da maxResults nella nuova versione actor
    }

    # Titoli di lavoro correnti — usa lista se fornita, altrimenti singolo ruolo
    titoli = [r for r in ruoli_lista if r][:5] if ruoli_lista else ([ruolo] if ruolo else [])
    if titoli:
        run_input["currentJobTitles"] = titoli

    # Keywords: combina ruoli + parole_chiave per migliorare la copertura di ricerca
    kw_parts = []
    if titoli:
        kw_parts.append(" OR ".join(f'"{t}"' for t in titoli[:3]))
    if parole_chiave:
        kw_parts.append(parole_chiave)
    if kw_parts:
        run_input["keywords"] = " ".join(kw_parts)

    # Location: normalizza città e aggiunge Italy se necessario
    if citta or paese:
        citta_norm = _normalizza_citta(citta) if citta else ""
        run_input["locations"] = [", ".join(filter(None, [citta_norm, paese]))] if paese else [citta_norm]
    elif forza_italia:
        run_input["locations"] = ["Italy"]

    if azienda:
        run_input["currentCompanies"] = [azienda]

    # Log diagnostico dell'input inviato ad Apify (visibile nei log Railway)
    log.info("APIFY INPUT: %s", json.dumps(run_input, ensure_ascii=False))
    import datetime
    print(f"=== APIFY REQUEST {datetime.datetime.now()} ===")
    print(json.dumps(run_input, indent=2, ensure_ascii=False))
    print("==========================================")

    # ── STEP 1: Avvia run (non blocca) ────────────────────────────────────────
    if progress_cb:
        progress_cb(0, "Avvio ricerca Apify")

    try:
        resp = requests.post(
            f"{APIFY_BASE}/acts/{APIFY_ACTOR}/runs",
            json=run_input,
            params={"token": api_key},
            timeout=30,
        )
        resp.raise_for_status()
        run_data   = resp.json()["data"]
        run_id     = run_data["id"]
        dataset_id = run_data["defaultDatasetId"]
    except requests.exceptions.HTTPError:
        return None, f"Errore avvio ricerca Apify: {resp.status_code} — {resp.text[:300]}"
    except requests.exceptions.RequestException as e:
        return None, f"Errore avvio ricerca: {str(e)}"

    # ── STEP 2: Poll ogni 5s — max 3 minuti ──────────────────────────────────
    if progress_cb:
        progress_cb(20, "Ricerca in corso su LinkedIn")

    max_wait      = 180   # 3 minuti
    poll_interval = 5
    elapsed       = 0

    while elapsed < max_wait:
        time.sleep(poll_interval)
        elapsed += poll_interval

        try:
            status_resp = requests.get(
                f"{APIFY_BASE}/actor-runs/{run_id}",
                params={"token": api_key},
                timeout=10,
            )
            status_resp.raise_for_status()
            run_status = status_resp.json()["data"]
            status     = run_status.get("status", "")

            if status == "SUCCEEDED":
                dataset_id = run_status.get("defaultDatasetId", dataset_id)
                break
            elif status in ("FAILED", "TIMED-OUT", "ABORTED"):
                return None, f"Ricerca Apify terminata con stato: {status}"
            # RUNNING / READY → continua il polling

        except requests.exceptions.RequestException:
            # Errore temporaneo di rete: riprova al prossimo ciclo
            pass
    else:
        return None, "Timeout: la ricerca ha impiegato più di 3 minuti. Riprova con criteri più specifici."

    # ── STEP 3: Recupera risultati dal dataset ────────────────────────────────
    if progress_cb:
        progress_cb(50, "Elaborazione risultati")

    try:
        items_resp = requests.get(
            f"{APIFY_BASE}/datasets/{dataset_id}/items",
            params={"token": api_key, "limit": 10},
            timeout=30,
        )
        items_resp.raise_for_status()
        items = items_resp.json()

        if isinstance(items, dict):
            items = items.get("items", [])
        if not isinstance(items, list):
            items = []
        print(f"=== APIFY RESPONSE: {len(items)} profili ===")
        for r in items[:3]:
            print(f"  - {r.get('fullName') or r.get('firstName','?')+' '+r.get('lastName','')} | {r.get('headline','?')} | {r.get('linkedinUrl','?')}")
        print("==========================================")
        return items, None

    except requests.exceptions.HTTPError:
        return None, f"Errore recupero risultati: {items_resp.status_code} — {items_resp.text[:200]}"
    except requests.exceptions.RequestException as e:
        return None, f"Errore recupero risultati: {str(e)}"


def _str(val) -> str:
    """
    Converte qualsiasi valore in stringa sicura per il database.
    Gestisce i casi in cui Apify restituisce oggetti annidati invece di stringhe:
    - dict con chiave "linkedinText", "name", "text" → estrae il testo
    - list → join con virgola
    - None / bool → stringa vuota o repr
    """
    if val is None:
        return ""
    if isinstance(val, str):
        return val
    if isinstance(val, dict):
        # Apify spesso restituisce {"linkedinText": "...", "text": "..."}
        for key in ("linkedinText", "text", "name", "value", "title"):
            if val.get(key) and isinstance(val[key], str):
                return val[key]
        return ""
    if isinstance(val, list):
        return ", ".join(_str(v) for v in val if v)
    return str(val)


def _costruisci_testo_profilo(p):
    """Costruisce il testo completo del profilo da usare per l'analisi AI e per la visualizzazione."""
    parti = [
        f"Nome: {_str(p.get('nome'))} {_str(p.get('cognome'))}".strip(),
        f"Ruolo: {_str(p.get('ruolo'))}",
        f"Azienda: {_str(p.get('azienda'))}",
        f"Location: {_str(p.get('location'))}",
    ]
    if p.get("sommario"):
        parti.append(f"Sommario: {_str(p['sommario'])}")
    if p.get("linkedin"):
        parti.append(f"LinkedIn: {_str(p['linkedin'])}")
    return "\n".join(parti)


# Termini che indicano location italiana (usati da _filtro_qualita)
_LOCATION_ITALIANE = {
    'italy', 'italia', 'milan', 'milano', 'roma', 'rome', 'napoli', 'naples',
    'torino', 'turin', 'firenze', 'florence', 'bologna', 'venezia', 'venice',
    'genova', 'genoa', 'palermo', 'bari', 'catania', 'verona', 'padova',
    'trieste', 'brescia', 'parma', 'modena', 'reggio', 'perugia', 'siena',
    'trento', 'trentino', 'sardegna', 'sicilia', 'lombardia', 'piemonte',
    'toscana', 'veneto', 'lazio', 'campania', 'puglia', 'calabria',
}


def _filtro_qualita(p: dict) -> tuple:
    """
    Scarta profili senza dati essenziali o non italiani.
    Va applicato PRIMA di _filtro_locale per eliminare il rumore di Apify.
    Ritorna (passa: bool, motivo_scarto: str).
    """
    # 1. Senza nome
    if not p.get('nome') and not p.get('cognome'):
        return False, "Senza nome"

    # 2. Senza ruolo attuale
    if not p.get('ruolo'):
        return False, "Senza ruolo attuale"

    # 3. Testo totale insufficiente
    testo = " ".join(filter(None, [
        p.get('nome', ''), p.get('cognome', ''), p.get('ruolo', ''),
        p.get('azienda', ''), p.get('sommario', ''),
    ]))
    if len(testo) < 50:
        return False, "Profilo incompleto (< 50 caratteri)"

    # 4. Location presente ma chiaramente non italiana
    location = (p.get('location') or '').lower()
    if location and not any(t in location for t in _LOCATION_ITALIANE):
        return False, f"Location non italiana: {p.get('location', '')[:40]}"

    return True, ""


# _controlla_duplicato rimossa: usa dedup.is_duplicate(db, profilo)


def _filtro_locale(p: dict, imp: dict) -> tuple:
    """
    Filtra velocemente un profilo normalizzato contro le impostazioni configurate.
    Non fa chiamate API — solo string matching locale.
    Ritorna (passa: bool, motivo_scarto: str).
    Più permissivo che restrittivo: se le impostazioni sono vuote, tutto passa.
    """
    testo = " ".join(filter(None, [
        str(p.get('ruolo', '') or ''),
        str(p.get('azienda', '') or ''),
        str(p.get('sommario', '') or ''),
    ])).lower()

    if not testo.strip() and not p.get('nome') and not p.get('cognome'):
        return False, "Profilo vuoto o senza dati"

    # 1. Keyword negative → scarto immediato
    kw_neg = [k.strip().lower() for k in (imp.get('keyword_negative', '') or '').split(',') if k.strip()]
    for kw in kw_neg:
        if kw in testo:
            return False, f"Keyword negativa: «{kw}»"

    # 2. Almeno un termine positivo (ruoli, settori/istituti, keyword pos) deve matchare
    ruoli    = [r.strip().lower() for r in (imp.get('ruoli_target', '') or '').split(',') if r.strip()]
    settori  = [s.strip().lower() for s in (imp.get('settori', '') or '').split(',') if s.strip()]
    istituti = [i.strip().lower() for i in (imp.get('istituti', '') or '').split(',') if i.strip()]
    kw_pos   = [k.strip().lower() for k in (imp.get('keyword_positive', '') or '').split(',') if k.strip()]

    positivi = ruoli + settori + istituti + kw_pos
    if positivi and testo:
        if not any(term in testo for term in positivi):
            return False, "Nessun ruolo/settore target rilevato nel profilo"

    return True, ""


def normalizza_profilo(p):
    """
    Estrae i campi utili da un profilo restituito dall'actor Apify.
    Usa _str() per garantire che tutti i valori siano stringhe,
    anche quando Apify restituisce oggetti annidati.
    """
    nome    = _str(p.get("firstName") or p.get("first_name") or "")
    cognome = _str(p.get("lastName")  or p.get("last_name")  or "")
    ruolo   = _str(p.get("headline") or p.get("title") or p.get("occupation") or "")

    # Azienda corrente: può essere in una lista di posizioni
    azienda = ""
    posizione_corrente = p.get("currentPositions") or p.get("positions") or []
    if posizione_corrente and isinstance(posizione_corrente, list):
        prima = posizione_corrente[0] if isinstance(posizione_corrente[0], dict) else {}
        azienda = _str(prima.get("companyName") or prima.get("company") or "")
        if not ruolo:
            ruolo = _str(prima.get("title") or "")

    # Fallback azienda da campo diretto
    if not azienda:
        azienda = _str(p.get("companyName") or p.get("company") or "")

    # location può essere {"linkedinText": "Milan, Italy"} o stringa diretta
    location = _str(p.get("location") or p.get("geoLocation") or "")
    linkedin  = _str(p.get("linkedinUrl") or p.get("profileUrl") or p.get("url") or "")
    summary_raw = p.get("summary") or p.get("about") or ""
    summary   = _str(summary_raw)[:200]

    return {
        "nome":     nome,
        "cognome":  cognome,
        "ruolo":    ruolo,
        "azienda":  azienda,
        "location": location,
        "linkedin": linkedin,
        "headline": ruolo,
        "sommario": summary,
    }


@ricerca_bp.route("/ricerca/scarta", methods=["POST"])
def scarta():
    """Aggiunge un profilo alla blacklist profili_scartati."""
    dati = request.get_json()
    db = get_db()
    db.execute(
        """INSERT INTO profili_scartati (linkedin_url, nome, cognome, ruolo, azienda, motivo)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (
            (dati.get("linkedin") or "").strip(),
            (dati.get("nome") or "").strip(),
            (dati.get("cognome") or "").strip(),
            (dati.get("ruolo") or "").strip(),
            (dati.get("azienda") or "").strip(),
            (dati.get("motivo") or "non_importato"),
        )
    )
    db.commit()
    db.close()
    log.info("Profilo scartato: %s %s (%s)", dati.get("nome"), dati.get("cognome"), dati.get("linkedin"))
    return jsonify({"successo": True})


@ricerca_bp.route("/ricerca")
def index():
    """Pagina di ricerca automatica figure con Apify/LinkedIn."""
    db = get_db()
    imp_a = db.execute("SELECT id FROM impostazioni_profilo WHERE profilo='A'").fetchone()
    imp_b = db.execute("SELECT id FROM impostazioni_profilo WHERE profilo='B'").fetchone()
    cronologia = db.execute(
        "SELECT * FROM ricerche_automatiche ORDER BY data_ricerca DESC"
    ).fetchall()
    db.close()
    cronologia = [dict(r) for r in cronologia]
    return render_template("ricerca.html",
                           imp_a_configurato=imp_a is not None,
                           imp_b_configurato=imp_b is not None,
                           cronologia=cronologia)


@ricerca_bp.route("/ricerca/cerca_nome", methods=["POST"])
def cerca_nome():
    """
    Cerca un profilo specifico su LinkedIn per nome e cognome.
    Usa Apify con keywords=nome+cognome senza currentJobTitles.
    Restituisce il primo risultato trovato.
    """
    dati = request.get_json()
    nome_cognome = (dati.get("nome_cognome") or "").strip()
    azienda      = (dati.get("azienda") or "").strip()
    tipo_profilo = dati.get("tipo_profilo", "A")

    if not nome_cognome:
        return jsonify({"errore": "Inserisci nome e cognome"}), 400

    api_key = os.environ.get("APIFY_API_KEY", "")
    if not api_key:
        return jsonify({"errore": "APIFY_API_KEY non configurata"}), 500

    # Costruisci input Apify: solo keywords, niente currentJobTitles
    run_input = {
        "takePages": 1,
        "startPage": 1,
        "maxItems": 5,
        "keywords": nome_cognome,
        "locations": ["Italy"],
    }
    if azienda:
        run_input["currentCompanies"] = [azienda]

    log.info("CERCA NOME: input=%s", json.dumps(run_input, ensure_ascii=False))
    print(f"=== CERCA NOME: {nome_cognome} (azienda={azienda}) ===", flush=True)

    # Avvia run Apify
    try:
        resp = requests.post(
            f"{APIFY_BASE}/acts/{APIFY_ACTOR}/runs",
            json=run_input,
            params={"token": api_key},
            timeout=30,
        )
        resp.raise_for_status()
        run_data   = resp.json()["data"]
        run_id     = run_data["id"]
        dataset_id = run_data["defaultDatasetId"]
    except requests.exceptions.RequestException as e:
        return jsonify({"errore": f"Errore Apify: {str(e)}"}), 500

    # Poll fino a completamento (max 2 minuti)
    max_wait = 120
    elapsed  = 0
    while elapsed < max_wait:
        time.sleep(5)
        elapsed += 5
        try:
            sr = requests.get(f"{APIFY_BASE}/actor-runs/{run_id}",
                              params={"token": api_key}, timeout=10)
            sr.raise_for_status()
            status = sr.json()["data"].get("status", "")
            if status == "SUCCEEDED":
                dataset_id = sr.json()["data"].get("defaultDatasetId", dataset_id)
                break
            elif status in ("FAILED", "TIMED-OUT", "ABORTED"):
                return jsonify({"errore": f"Ricerca terminata: {status}"}), 500
        except requests.exceptions.RequestException:
            pass
    else:
        return jsonify({"errore": "Timeout ricerca (2 min)"}), 500

    # Recupera risultati
    try:
        items_resp = requests.get(
            f"{APIFY_BASE}/datasets/{dataset_id}/items",
            params={"token": api_key, "limit": 5},
            timeout=30,
        )
        items_resp.raise_for_status()
        items = items_resp.json()
        if isinstance(items, dict):
            items = items.get("items", [])
    except requests.exceptions.RequestException as e:
        return jsonify({"errore": f"Errore recupero risultati: {str(e)}"}), 500

    if not items:
        return jsonify({"errore": "Nessun profilo trovato per questo nome"}), 404

    # Normalizza e restituisci il primo profilo
    profilo = normalizza_profilo(items[0])
    profilo["tipo_profilo"] = tipo_profilo

    # Salva nella cronologia
    parametri_str = json.dumps({'nome_cognome': nome_cognome, 'azienda': azienda},
                               ensure_ascii=False)
    db = get_db()
    cur = db.execute(
        """INSERT INTO ricerche_automatiche
           (tipo_profilo, parametri, profili_trovati, profili_importati, fonte, stato)
           VALUES (?, ?, 1, 0, 'manuale', 'completata')""",
        (tipo_profilo, parametri_str)
    )
    ricerca_id = cur.lastrowid
    testo = _costruisci_testo_profilo(profilo)
    cur_p = db.execute(
        """INSERT INTO profili_ricerca
           (ricerca_id, nome, cognome, ruolo, azienda, location, linkedin_url, testo_profilo)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (ricerca_id, profilo["nome"], profilo["cognome"], profilo["ruolo"],
         profilo["azienda"], profilo["location"], profilo["linkedin"], testo)
    )
    profilo["profilo_ricerca_id"] = cur_p.lastrowid
    profilo["ricerca_id"] = ricerca_id
    db.commit()
    db.close()

    print(f"=== CERCA NOME OK: {profilo['nome']} {profilo['cognome']} — {profilo['ruolo']} ===", flush=True)
    return jsonify({"profilo": profilo, "ricerca_id": ricerca_id})


@ricerca_bp.route("/ricerca/cerca", methods=["POST"])
def cerca():
    """
    Esegue la ricerca su Apify con retry automatico se i nuovi profili sono < 3.
    Fa fino a MAX_TENTATIVI chiamate ad Apify incrementando startPage ad ogni retry.
    Salva UNA sola ricerca nella cronologia con i profili aggregati di tutti i tentativi.
    """
    _MIN_NUOVI      = 10  # obiettivo: almeno 10 profili nuovi
    _MAX_TENTATIVI  = 5   # max tentativi con il ruolo originale, poi si passa ai correlati

    dati = request.get_json()
    ruolo         = dati.get("ruolo", "").strip()
    citta         = dati.get("citta", "").strip()
    paese         = dati.get("paese", "").strip()
    azienda       = dati.get("azienda", "").strip()
    parole_chiave = dati.get("parole_chiave", "").strip()
    num_pagine    = int(dati.get("num_pagine", 1))
    tipo_profilo  = dati.get("tipo_profilo", "A")

    if not ruolo and not parole_chiave:
        return jsonify({"errore": "Inserisci almeno il ruolo o delle parole chiave"}), 400

    # Calcola startPage iniziale: ruota tra ricerche diverse per variare i risultati
    db_cnt = get_db()
    n_precedenti = db_cnt.execute(
        "SELECT COUNT(*) AS n FROM ricerche_automatiche WHERE fonte='manuale'",
    ).fetchone()["n"] or 0
    db_cnt.close()
    # Ruota solo tra pagine 1, 2, 3 — niente pagine alte dove Apify non ha risultati
    start_page_iniziale = (n_precedenti % 3) + 1

    log.info("Ricerca manuale: ruolo=%r citta=%r start_page_iniziale=%d",
             ruolo, citta, start_page_iniziale)
    print(f"=== CERCA START: ruolo={ruolo} citta={citta} ===", flush=True)

    # ── Retry loop ─────────────────────────────────────────────────────────────
    db_check = get_db()
    tutti_profili   = []   # tutti i profili unici trovati in tutti i tentativi
    tutti_linkedin  = set()  # linkedin_url già visti in questo run (dedup cross-attempt)
    tutti_nomi      = set()  # "nome cognome" già visti in questo run
    tentativi_fatti = 0
    primo_errore    = None
    _sp_base        = start_page_iniziale  # base resettabile per il calcolo di startPage
    nuovi_per_ruolo = {}  # {ruolo_usato: n_nuovi} per il messaggio riepilogo

    for tentativo in range(1, _MAX_TENTATIVI + 1):
        start_page = min(_sp_base + (tentativo - 1), 5)  # cap assoluto a 5
        tentativi_fatti = tentativo

        print(f"=== TENTATIVO {tentativo}: startPage={start_page} ===", flush=True)
        log.info("Tentativo %d/%d: ruolo=%r citta=%r start_page=%d",
                 tentativo, _MAX_TENTATIVI, ruolo, citta, start_page)

        items, errore = cerca_apify(ruolo, citta, paese, azienda, parole_chiave, num_pagine,
                                    start_page=start_page)

        if errore:
            log.warning("Tentativo %d fallito: %s", tentativo, errore)
            if tentativo == 1:
                primo_errore = errore  # salva per ritornarlo se anche gli altri falliscono
            continue

        trovati_questo_tentativo = 0
        nuovi_questo_tentativo   = 0

        for item in items:
            if not isinstance(item, dict):
                continue
            p = normalizza_profilo(item)

            # Dedup cross-attempt: salta se già visto in un tentativo precedente
            li = (p.get("linkedin") or "").strip().lower()
            nk = f"{p.get('nome','').strip()} {p.get('cognome','').strip()}".strip().lower()

            if li and li in tutti_linkedin:
                continue
            if nk and nk in tutti_nomi and not li:
                continue

            trovati_questo_tentativo += 1
            if li:
                tutti_linkedin.add(li)
            if nk:
                tutti_nomi.add(nk)

            dup, motivo_dup, cand_id = is_duplicate(db_check, p)
            p["gia_in_pipeline"] = dup
            p["candidato_id_esistente"] = cand_id

            nome_log = f"{p.get('nome','')} {p.get('cognome','')}".strip() or "?"
            print(f"  DEDUP: {nome_log} | linkedin={p.get('linkedin','—')[:50]} | dup={dup} | motivo={motivo_dup}", flush=True)
            log.info("  Profilo %s: linkedin=%s — già in DB: %s%s",
                     nome_log, p.get("linkedin", "—"),
                     "SI" if dup else "NO",
                     f" ({motivo_dup})" if dup else "")
            tutti_profili.append(p)
            if not dup:
                nuovi_questo_tentativo += 1
                nuovi_per_ruolo[ruolo] = nuovi_per_ruolo.get(ruolo, 0) + 1

        nuovi_totale = sum(1 for q in tutti_profili if not q["gia_in_pipeline"])
        print(f"=== TENTATIVO {tentativo}: trovati={trovati_questo_tentativo} nuovi={nuovi_questo_tentativo} totale_nuovi={nuovi_totale} ===", flush=True)
        log.info("Tentativo %d: trovati=%d nuovi=%d totale_nuovi=%d",
                 tentativo, trovati_questo_tentativo, nuovi_questo_tentativo, nuovi_totale)

        if nuovi_totale >= _MIN_NUOVI:
            log.info("Raggiunta soglia %d nuovi — stop ai tentativi", _MIN_NUOVI)
            break

        # 0 profili trovati su pagina alta → reset a startPage=1 per il prossimo tentativo
        if trovati_questo_tentativo == 0 and start_page >= 5 and tentativo < _MAX_TENTATIVI:
            _sp_base = 1 - tentativo  # garantisce che il prossimo tentativo parta da page 1
            print(f"=== TENTATIVO {tentativo}: 0 trovati su pagina alta ({start_page}), reset a startPage=1 ===", flush=True)
            log.info("Tentativo %d: 0 trovati su pagina alta %d, reset startPage a 1", tentativo, start_page)

        # 0 profili nuovi in questo tentativo: log esplicito e continua
        if nuovi_questo_tentativo == 0 and tentativo < _MAX_TENTATIVI:
            next_page = min(_sp_base + tentativo, 5)
            print(f"Tentativo {tentativo}: 0 nuovi trovati, riprovo con startPage={next_page}", flush=True)
            log.info("Tentativo %d: 0 nuovi trovati, riprovo con startPage=%d", tentativo, next_page)

    db_check.close()

    # Se tutti i tentativi hanno fallito con errore e non abbiamo nulla
    if not tutti_profili and primo_errore:
        return jsonify({"errore": primo_errore}), 500

    # ── FASE 2: ruoli correlati — attivata se < _MIN_NUOVI dopo il loop principale ──
    nuovi_totale_dopo_main = sum(1 for q in tutti_profili if not q["gia_in_pipeline"])
    ruoli_correlati_usati  = []

    if nuovi_totale_dopo_main < _MIN_NUOVI and ruolo:
        correlati = RUOLI_CORRELATI.get(ruolo.lower().strip(), [])
        print(f"=== FASE 2: {nuovi_totale_dopo_main} nuovi con '{ruolo}' (obiettivo {_MIN_NUOVI}), provo correlati: {correlati} ===", flush=True)
        log.info("Fase 2: %d nuovi con ruolo=%r — provo correlati: %s", nuovi_totale_dopo_main, ruolo, correlati)

        db_corr = get_db()
        for ruolo_corr in correlati:
            if nuovi_totale_dopo_main >= _MIN_NUOVI:
                break
            for sp in (1, 2):
                if nuovi_totale_dopo_main >= _MIN_NUOVI:
                    break
                print(f"=== CORRELATO '{ruolo_corr}' startPage={sp} ===", flush=True)
                log.info("Correlato '%s' startPage=%d", ruolo_corr, sp)
                items_c, err_c = cerca_apify(ruolo_corr, citta, paese, azienda, parole_chiave,
                                             num_pagine, start_page=sp)
                if err_c:
                    log.warning("Correlato '%s' pag %d errore: %s", ruolo_corr, sp, err_c)
                    continue

                nuovi_corr = 0
                for item in (items_c or []):
                    if not isinstance(item, dict):
                        continue
                    p = normalizza_profilo(item)
                    li = (p.get("linkedin") or "").strip().lower()
                    nk = f"{p.get('nome','').strip()} {p.get('cognome','').strip()}".strip().lower()
                    if li and li in tutti_linkedin:
                        continue
                    if nk and nk in tutti_nomi and not li:
                        continue
                    if li:
                        tutti_linkedin.add(li)
                    if nk:
                        tutti_nomi.add(nk)
                    dup, _, cand_id = is_duplicate(db_corr, p)
                    p["gia_in_pipeline"] = dup
                    p["candidato_id_esistente"] = cand_id
                    p["ruolo_ricerca"] = ruolo_corr
                    tutti_profili.append(p)
                    if not dup:
                        nuovi_corr += 1
                        nuovi_per_ruolo[ruolo_corr] = nuovi_per_ruolo.get(ruolo_corr, 0) + 1

                nuovi_totale_dopo_main = sum(1 for q in tutti_profili if not q["gia_in_pipeline"])
                print(f"=== CORRELATO '{ruolo_corr}' pag {sp}: nuovi_questo={nuovi_corr} totale_nuovi={nuovi_totale_dopo_main} ===", flush=True)

                if ruolo_corr not in ruoli_correlati_usati:
                    ruoli_correlati_usati.append(ruolo_corr)

        db_corr.close()

    # ── FASE 3: fallback keyword generiche ─────────────────────────────────────
    nuovi_totale_dopo_fase2 = sum(1 for q in tutti_profili if not q["gia_in_pipeline"])
    keywords_generiche_usate = []

    if nuovi_totale_dopo_fase2 < _MIN_NUOVI:
        print(f"=== FASE 3: {nuovi_totale_dopo_fase2} nuovi, provo keyword generiche ===", flush=True)
        log.info("Fase 3: %d nuovi — provo keyword generiche", nuovi_totale_dopo_fase2)

        db_kw = get_db()
        for kw in _KW_GENERICHE_FALLBACK:
            if nuovi_totale_dopo_fase2 >= _MIN_NUOVI:
                break
            for sp in (1, 2):
                if nuovi_totale_dopo_fase2 >= _MIN_NUOVI:
                    break
                print(f"=== KW_GENERICA '{kw}' startPage={sp} ===", flush=True)
                log.info("KW generica '%s' startPage=%d", kw, sp)
                items_k, err_k = cerca_apify("", citta, paese, "", kw, num_pagine, start_page=sp)
                if err_k:
                    log.warning("KW generica '%s' pag %d errore: %s", kw, sp, err_k)
                    continue

                nuovi_kw = 0
                for item in (items_k or []):
                    if not isinstance(item, dict):
                        continue
                    p = normalizza_profilo(item)
                    li = (p.get("linkedin") or "").strip().lower()
                    nk = f"{p.get('nome','').strip()} {p.get('cognome','').strip()}".strip().lower()
                    if li and li in tutti_linkedin:
                        continue
                    if nk and nk in tutti_nomi and not li:
                        continue
                    if li:
                        tutti_linkedin.add(li)
                    if nk:
                        tutti_nomi.add(nk)
                    dup, _, cand_id = is_duplicate(db_kw, p)
                    p["gia_in_pipeline"] = dup
                    p["candidato_id_esistente"] = cand_id
                    p["ruolo_ricerca"] = kw
                    tutti_profili.append(p)
                    if not dup:
                        nuovi_kw += 1
                        nuovi_per_ruolo[kw] = nuovi_per_ruolo.get(kw, 0) + 1

                nuovi_totale_dopo_fase2 = sum(1 for q in tutti_profili if not q["gia_in_pipeline"])
                print(f"=== KW '{kw}' pag {sp}: nuovi_questo={nuovi_kw} totale_nuovi={nuovi_totale_dopo_fase2} ===", flush=True)

                if kw not in keywords_generiche_usate and nuovi_kw > 0:
                    keywords_generiche_usate.append(kw)

        db_kw.close()

    # ── Calcola statistiche finali ─────────────────────────────────────────────
    gia_presenti = sum(1 for p in tutti_profili if p["gia_in_pipeline"])
    nuovi        = len(tutti_profili) - gia_presenti

    log.info("Ricerca manuale completata: tentativi=%d trovati=%d gia_presenti=%d nuovi=%d correlati=%s",
             tentativi_fatti, len(tutti_profili), gia_presenti, nuovi, ruoli_correlati_usati)
    print(f"=== RISULTATO FINALE: tentativi={tentativi_fatti} trovati={len(tutti_profili)} "
          f"nuovi={nuovi} gia_presenti={gia_presenti} correlati={ruoli_correlati_usati} ===")

    # ── Salva in DB (una sola ricerca per tutti i tentativi) ───────────────────
    parametri_str = json.dumps({
        'ruolo': ruolo, 'citta': citta, 'azienda': azienda,
        'parole_chiave': parole_chiave, 'start_page': start_page_iniziale,
        'tentativi': tentativi_fatti,
    }, ensure_ascii=False)
    db = get_db()
    cur = db.execute(
        """INSERT INTO ricerche_automatiche
           (tipo_profilo, parametri, profili_trovati, profili_importati, fonte, stato)
           VALUES (?, ?, ?, 0, 'manuale', 'completata')""",
        (tipo_profilo, parametri_str, len(tutti_profili))
    )
    ricerca_id = cur.lastrowid

    for p in tutti_profili:
        testo = _costruisci_testo_profilo(p)
        cur_p = db.execute(
            """INSERT INTO profili_ricerca
               (ricerca_id, nome, cognome, ruolo, azienda, location, linkedin_url, testo_profilo)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (ricerca_id, p["nome"], p["cognome"], p["ruolo"], p["azienda"],
             p["location"], p["linkedin"], testo)
        )
        p["profilo_ricerca_id"] = cur_p.lastrowid

    db.commit()
    db.close()

    # ── Messaggio finale sempre informativo ────────────────────────────────────
    fasi_usate = []
    if ruoli_correlati_usati:
        fasi_usate.append("correlati")
    if keywords_generiche_usate:
        fasi_usate.append("ricerca generica")

    if nuovi == 0:
        messaggio = "Nessun profilo disponibile. Prova a cambiare città."
        print(f"=== ATTENZIONE: 0 nuovi dopo tutte le fasi (correlati={ruoli_correlati_usati} kw={keywords_generiche_usate}) ===", flush=True)
        log.warning("0 nuovi dopo tutte le fasi — correlati: %s, kw: %s", ruoli_correlati_usati, keywords_generiche_usate)
    else:
        # Riepilogo sempre: "X nuovi profili trovati: N da 'ruolo', M da 'correlato'..."
        parti = [f"{n} da '{r}'" for r, n in nuovi_per_ruolo.items() if n > 0]
        if len(parti) > 1:
            messaggio = f"{nuovi} nuovi profili trovati: {', '.join(parti)}"
        else:
            messaggio = f"{nuovi} nuov{'o profilo' if nuovi == 1 else 'i profili'} trovat{'o' if nuovi == 1 else 'i'}"
        if fasi_usate:
            messaggio += f" (usando anche {' e '.join(fasi_usate)})"

    return jsonify({
        "persone":                  tutti_profili,
        "totale":                   len(tutti_profili),
        "gia_presenti":             gia_presenti,
        "nuovi":                    nuovi,
        "start_page":               start_page_iniziale,
        "ricerca_id":               ricerca_id,
        "tentativi_fatti":          tentativi_fatti,
        "messaggio":                messaggio,
        "ruoli_correlati_usati":    ruoli_correlati_usati,
        "keywords_generiche_usate": keywords_generiche_usate,
    })


def _esegui_ricerca_background(job_id, tipo_profilo, max_profili, imp):
    """Thread daemon: esegue la ricerca Apify + analisi AI in background."""
    import logging
    log = logging.getLogger(__name__)
    db = get_db()

    def aggiorna(status=None, step=None, pct=None):
        sets, vals = [], []
        if status is not None:
            sets.append("status=?"); vals.append(status)
        if step is not None:
            sets.append("step=?"); vals.append(step)
        if pct is not None:
            sets.append("percentuale=?"); vals.append(pct)
        vals.append(job_id)
        db.execute("UPDATE job_ricerche SET " + ", ".join(sets) + " WHERE job_id=?", vals)
        db.commit()

    try:
        aggiorna(status='in_corso', step='Avvio ricerca Apify', pct=0)

        ruoli_raw     = imp.get("ruoli_target", "") or ""
        ruoli         = [r.strip() for r in ruoli_raw.split(",") if r.strip()]
        kw_positive   = imp.get("keyword_positive", "") or ""
        extra_settore = (imp.get("settori", "") if tipo_profilo == "A" else imp.get("istituti", "")) or ""
        kw_parts      = [k.strip() for k in kw_positive.split(",") if k.strip()]
        kw_parts     += [s.strip() for s in extra_settore.split(",") if s.strip()]

        # ── Leggi cursori offset dal DB e pre-calcola params per 3 tentativi ──
        citta_lista = _CITTA_ROTAZIONE.get(tipo_profilo, ['Italy'])
        off_row = db.execute(
            "SELECT * FROM search_offset WHERE tipo_profilo=?", (tipo_profilo,)
        ).fetchone()
        if not off_row:
            db.execute(
                "INSERT INTO search_offset (tipo_profilo, offset_corrente, indice_ruolo, indice_citta) VALUES (?, 0, 0, 0)",
                (tipo_profilo,)
            )
            db.commit()
            _off, _ir, _ic = 0, 0, 0
        else:
            _off = off_row['offset_corrente']
            _ir  = off_row['indice_ruolo']
            _ic  = off_row['indice_citta']

        _nr = max(len(ruoli), 1)
        _nc = len(citta_lista)

        # Città fissa per tutta la sessione di ricerca (ruota tra ricerche diverse)
        citta_corrente  = citta_lista[_ic % _nc]
        ruolo_principale = ruoli[_ir % _nr] if ruoli else ""
        start_page_base  = (_off // 10) + 1   # 1..10

        # Aggiorna i cursori per la PROSSIMA ricerca (una sola volta)
        db.execute(
            """UPDATE search_offset SET
               offset_corrente=?, indice_ruolo=?, indice_citta=?,
               ultimo_aggiornamento=CURRENT_TIMESTAMP
               WHERE tipo_profilo=?""",
            ((_off + 10) % 100, (_ir + 1) % _nr, (_ic + 1) % _nc, tipo_profilo)
        )
        db.commit()

        keywords   = " ".join(kw_parts[:8]) if kw_parts else ""
        num_pagine = max(1, (max_profili * 2 + 9) // 10)

        parametri_str = json.dumps({
            'ruolo': ruolo_principale, 'citta': citta_corrente,
            'keywords': keywords, 'max_profili': max_profili,
            'start_page': start_page_base,
        }, ensure_ascii=False)

        trovati_apify    = 0
        scartati_qualita = 0
        scartati_filtro  = 0
        gia_presenti     = 0
        importati        = 0
        valutati         = 0
        punteggi: list        = []
        motivi_qualita: dict  = {}
        motivi_filtro:  dict  = {}

        items_da_importare = []
        seen_batch         = set()   # dedup intra-batch tra tentativi diversi
        MAX_TENTATIVI      = 5
        SOGLIA_NUOVI       = 10

        # ── Fino a MAX_TENTATIVI: città fissa, ruolo e startPage crescono ─────
        # Tentativo 1: ruolo[0], pag. base
        # Tentativo 2: ruolo[1], pag. base+1   (più risultati, stesso contesto)
        # Tentativo 3: ruolo[2], pag. base+2
        # ...
        for tentativo in range(1, MAX_TENTATIVI + 1):
            if len(items_da_importare) >= SOGLIA_NUOVI:
                break

            ruolo_t      = ruoli[(_ir + tentativo - 1) % _nr] if ruoli else ""
            start_page_t = (start_page_base + tentativo - 1 - 1) % 10 + 1  # 1-indexed, mod 10

            aggiorna(
                step=f'Tentativo {tentativo}/{MAX_TENTATIVI}: "{ruolo_t}" in {citta_corrente} (pag. {start_page_t})...',
                pct=5 + tentativo * 10,
            )
            log.info("Tentativo %d/%d: ruolo=%r citta=%r start_page=%d",
                     tentativo, MAX_TENTATIVI, ruolo_t, citta_corrente, start_page_t)

            items, errore = cerca_apify(
                ruolo_t, citta_corrente, "", "", keywords, num_pagine,
                ruoli_lista=[ruolo_t] if ruolo_t else [],
                forza_italia=False,
                progress_cb=None,
                start_page=start_page_t,
            )

            if errore:
                log.warning("Tentativo %d fallito: %s", tentativo, errore)
                if tentativo == 1:
                    # Primo tentativo fallito: registra errore e termina
                    db.execute(
                        "INSERT INTO ricerche_automatiche (tipo_profilo, parametri, profili_trovati, profili_importati, stato) VALUES (?, ?, 0, 0, 'errore')",
                        (tipo_profilo, parametri_str)
                    )
                    db.commit()
                    aggiorna(status='errore', step=errore)
                    return
                continue

            trovati_t    = len(items or [])
            trovati_apify += trovati_t
            importati_t  = 0

            for item in (items or []):
                if len(items_da_importare) >= max_profili:
                    break
                if not isinstance(item, dict):
                    continue
                p = normalizza_profilo(item)

                ok_q, motivo_q = _filtro_qualita(p)
                if not ok_q:
                    scartati_qualita += 1
                    motivi_qualita[motivo_q] = motivi_qualita.get(motivo_q, 0) + 1
                    continue

                ok_l, motivo_l = _filtro_locale(p, imp)
                if not ok_l:
                    scartati_filtro += 1
                    motivi_filtro[motivo_l] = motivi_filtro.get(motivo_l, 0) + 1
                    continue

                dup, motivo_dup, _ = is_duplicate(db, p)
                if dup:
                    gia_presenti += 1
                    log.info("Scartato duplicato: %s", motivo_dup)
                    continue

                # Dedup intra-batch: evita doppioni tra tentativi diversi
                chiave = (p.get('nome', '').lower().strip(), p.get('cognome', '').lower().strip())
                if chiave in seen_batch:
                    continue
                seen_batch.add(chiave)

                items_da_importare.append(p)
                importati_t += 1

            log.info("Tentativo %d/%d: trovati=%d importati_nuovi=%d (totale_nuovi=%d)",
                     tentativo, MAX_TENTATIVI, trovati_t, importati_t, len(items_da_importare))

            if importati_t == 0 and tentativo < MAX_TENTATIVI:
                next_page_t = (start_page_base + tentativo - 1) % 10 + 1
                print(f"Tentativo {tentativo}: 0 nuovi trovati, riprovo con startPage={next_page_t}", flush=True)
                log.info("Tentativo %d: 0 nuovi trovati, riprovo con startPage=%d", tentativo, next_page_t)

            aggiorna(
                step=f'Tentativo {tentativo}/{MAX_TENTATIVI}: trovati {trovati_t}, nuovi {importati_t}. Totale: {len(items_da_importare)}',
                pct=min(85, 10 + tentativo * 15),
            )

        trovati_filtrati = len(items_da_importare)

        msg_zero = ""
        if trovati_filtrati == 0:
            msg_zero = (
                "Tutti i profili disponibili per questa ricerca sono già in pipeline. "
                "Prova a cambiare ruolo o città."
            )
            print(f"=== ATTENZIONE: {MAX_TENTATIVI} tentativi completati, 0 nuovi trovati ===", flush=True)
            log.warning("Tutti i %d tentativi esauriti senza nuovi profili (tipo=%s)", MAX_TENTATIVI, tipo_profilo)
        aggiorna(step=f'Filtraggio completato. Salvataggio {trovati_filtrati} candidati...', pct=90)

        cur_r = db.execute(
            "INSERT INTO ricerche_automatiche (tipo_profilo, parametri, profili_trovati, fonte, stato) VALUES (?, ?, ?, 'apify', 'in_corso')",
            (tipo_profilo, parametri_str, trovati_apify)
        )
        ricerca_id = cur_r.lastrowid
        db.commit()

        for p in items_da_importare:
            testo = _costruisci_testo_profilo(p)
            _gestore = "Salvatore Sabia" if tipo_profilo == "A" else ("Firdaous Filahi" if tipo_profilo == "B" else "Non assegnato")
            cur = db.execute(
                "INSERT INTO candidati (nome, cognome, ruolo_attuale, azienda, profilo_linkedin, tipo_profilo, stato, note, ricerca_id, gestore) VALUES (?, ?, ?, ?, ?, ?, 'Da valutare', ?, ?, ?)",
                (p["nome"], p["cognome"], p["ruolo"], p["azienda"], p["linkedin"], tipo_profilo, p["headline"], ricerca_id, _gestore)
            )
            db.commit()
            candidato_id = cur.lastrowid
            importati += 1

            db.execute(
                "INSERT INTO profili_ricerca (ricerca_id, nome, cognome, ruolo, azienda, location, linkedin_url, testo_profilo, candidato_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (ricerca_id, p["nome"], p["cognome"], p["ruolo"], p["azienda"], p["location"], p["linkedin"], testo, candidato_id)
            )
            db.commit()

            if valutati < 10:
                _ai_total = min(len(items_da_importare), 10)
                aggiorna(step=f'Salvataggio database — analisi AI candidato {valutati + 1}/{_ai_total}...', pct=90)
                try:
                    risultato     = analizza_profilo_linkedin(testo, tipo_profilo, imp)
                    punteggio     = int(risultato.get("punteggio") or 0) or None
                    spunti_raw    = risultato.get("spunti_contatto", [])
                    spunti_json   = json.dumps(spunti_raw if isinstance(spunti_raw, list) else [], ensure_ascii=False)
                    analisi_str   = str(risultato.get("analisi_percorso") or "")
                    messaggio_str = str(risultato.get("messaggio_outreach") or "")
                    db.execute(
                        "UPDATE candidati SET punteggio=?, analisi=?, spunti=?, messaggio_outreach=?, stato='Da contattare', data_aggiornamento=CURRENT_TIMESTAMP WHERE id=?",
                        (punteggio, analisi_str, spunti_json, messaggio_str, candidato_id)
                    )
                    nome_completo = f"{p['nome']} {p['cognome']}".strip() or None
                    anteprima     = f"{p['nome']} {p['cognome']} — {p['ruolo']}".strip()[:120]
                    db.execute(
                        "INSERT INTO valutazioni (nome_contatto, ruolo_attuale, azienda, tipo_profilo, anteprima_testo, punteggio, analisi, spunti, messaggio_outreach, candidato_id, fonte) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (nome_completo, p['ruolo'] or None, p['azienda'] or None, tipo_profilo, anteprima, punteggio, analisi_str, spunti_json, messaggio_str, candidato_id, 'ricerca_automatica')
                    )
                    db.commit()
                    valutati += 1
                    if punteggio:
                        punteggi.append(punteggio)
                except Exception:
                    log.exception("Errore analisi AI per %s %s", p.get('nome'), p.get('cognome'))

        punteggio_medio = round(sum(punteggi) / len(punteggi), 1) if punteggi else None
        db.execute(
            "UPDATE ricerche_automatiche SET profili_importati=?, punteggio_medio=?, stato='completata' WHERE id=?",
            (importati, punteggio_medio, ricerca_id)
        )
        db.commit()

        risultati_json = json.dumps({
            # ── 4 numeri principali del riepilogo ─────────────────────────────
            "trovati_apify":    trovati_apify,       # Trovati da Apify
            "gia_presenti":     gia_presenti,         # Già presenti (scartati)
            "non_in_target":    scartati_qualita + scartati_filtro,  # Non in target (filtrati)
            "importati":        importati,            # Importati nuovi
            # ── Dettaglio aggiuntivo ───────────────────────────────────────────
            "filtrati":         trovati_filtrati,
            "valutati":         valutati,
            "punteggio_medio":  punteggio_medio,
            "scartati_qualita": scartati_qualita,
            "scartati_filtro":  scartati_filtro,
            "motivi_qualita":   motivi_qualita,
            "motivi_filtro":    motivi_filtro,
            "messaggio":        msg_zero,
        }, ensure_ascii=False)

        step_finale = msg_zero if msg_zero else 'Completato'
        db.execute(
            "UPDATE job_ricerche SET status='completato', step=?, risultati=?, percentuale=100, data_fine=CURRENT_TIMESTAMP WHERE job_id=?",
            (step_finale, risultati_json, job_id)
        )
        db.commit()

    except Exception as e:
        log.exception("Errore background job=%s", job_id)
        try:
            db.execute(
                "UPDATE job_ricerche SET status='errore', errore=?, step=?, data_fine=CURRENT_TIMESTAMP WHERE job_id=?",
                (str(e), f"Errore: {str(e)[:200]}", job_id)
            )
            db.commit()
        except Exception:
            pass
    finally:
        db.close()


@ricerca_bp.route("/ricerca/automatica", methods=["POST"])
def automatica():
    """Avvia la ricerca automatica in background e restituisce subito un job_id."""
    dati         = request.get_json()
    tipo_profilo = dati.get("tipo_profilo", "A")
    max_profili  = max(1, min(int(dati.get("max_profili", 20)), 100))

    db = get_db()
    imp_row = db.execute(
        "SELECT * FROM impostazioni_profilo WHERE profilo = ?", (tipo_profilo,)
    ).fetchone()
    db.close()

    if not imp_row:
        return jsonify({"errore": f"Impostazioni Profilo {tipo_profilo} non configurate. "
                                  f"Vai in Impostazioni e salva i parametri."}), 400

    imp    = dict(imp_row)
    job_id = str(uuid.uuid4())

    db2 = get_db()
    db2.execute(
        "INSERT INTO job_ricerche (job_id, tipo_profilo, status, step) VALUES (?, ?, 'avviato', ?)",
        (job_id, tipo_profilo, 'Preparazione ricerca...')
    )
    db2.commit()
    db2.close()

    t = threading.Thread(
        target=_esegui_ricerca_background,
        args=(job_id, tipo_profilo, max_profili, imp),
        daemon=True
    )
    t.start()

    return jsonify({"job_id": job_id, "status": "avviato"})


@ricerca_bp.route("/ricerca/stato/<job_id>")
def stato_job(job_id):
    """Restituisce lo stato attuale di un job di ricerca."""
    db  = get_db()
    row = db.execute("SELECT * FROM job_ricerche WHERE job_id = ?", (job_id,)).fetchone()
    db.close()
    if not row:
        return jsonify({"errore": "Job non trovato"}), 404
    row = dict(row)
    if row.get("risultati"):
        try:
            row["risultati"] = json.loads(row["risultati"])
        except Exception:
            pass
    return jsonify(row)


@ricerca_bp.route("/ricerca/export_csv")
def export_csv():
    """Esporta la cronologia ricerche automatiche in formato CSV."""
    db = get_db()
    rows = db.execute(
        "SELECT * FROM ricerche_automatiche ORDER BY data_ricerca DESC"
    ).fetchall()
    db.close()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        'ID', 'Data', 'Tipo Profilo', 'Profili Trovati',
        'Importati', 'Punteggio Medio', 'Stato', 'Parametri'
    ])
    for r in rows:
        writer.writerow([
            r['id'],
            r['data_ricerca'],
            r['tipo_profilo'],
            r['profili_trovati'],
            r['profili_importati'],
            r['punteggio_medio'] if r['punteggio_medio'] else '',
            r['stato'],
            r['parametri'] or '',
        ])

    return Response(
        '\ufeff' + output.getvalue(),  # BOM per compatibilità Excel
        mimetype='text/csv; charset=utf-8',
        headers={'Content-Disposition': 'attachment; filename=cronologia_ricerche.csv'}
    )


@ricerca_bp.route("/ricerca/analizza_candidato", methods=["POST"])
def analizza_candidato():
    """
    Esegue analisi AI su un candidato. Gestisce tre casi:
    1. candidato_id → candidato già in pipeline (re-analisi o prima analisi)
    2. profilo_ricerca_id → profilo salvato in profili_ricerca ma non ancora in pipeline
    3. dati testuali diretti → candidato completamente nuovo
    Salva automaticamente in candidati (pipeline) e valutazioni (cronologia).
    """
    print(f"=== ROUTE HIT: {request.path} ===")
    dati = request.get_json()
    candidato_id          = dati.get("candidato_id")
    profilo_ricerca_id    = dati.get("profilo_ricerca_id")
    tipo_profilo          = dati.get("tipo_profilo", "A")
    ricerca_id            = dati.get("ricerca_id")
    risultato_precomputed = dati.get("risultato_precomputed")  # se già calcolato dal frontend (SSE)
    dati_arricchiti_json  = dati.get("dati_arricchiti")        # JSON string con campi arricchiti

    db = get_db()

    if candidato_id:
        # Caso 1: candidato già in DB (pipeline)
        c = db.execute(
            "SELECT * FROM candidati WHERE id = ?", (candidato_id,)
        ).fetchone()
        if not c:
            db.close()
            return jsonify({"errore": "Candidato non trovato"}), 404
        tipo_profilo  = c["tipo_profilo"]
        ricerca_id    = c["ricerca_id"]
        # Cerca il testo profilo in profili_ricerca se disponibile
        pr = db.execute(
            "SELECT testo_profilo FROM profili_ricerca WHERE candidato_id = ? LIMIT 1",
            (candidato_id,)
        ).fetchone()
        if pr and pr.get("testo_profilo"):
            testo_profilo = pr["testo_profilo"]
        else:
            testo_profilo = (
                f"Nome: {c['nome']} {c['cognome']}\n"
                f"Ruolo: {c['ruolo_attuale'] or ''}\n"
                f"Azienda: {c['azienda'] or ''}\n"
            )
            if c.get("profilo_linkedin"):
                testo_profilo += f"LinkedIn: {c['profilo_linkedin']}\n"
        nome    = c["nome"]
        cognome = c["cognome"]
        ruolo   = c["ruolo_attuale"] or ""
        azienda = c["azienda"] or ""
        linkedin = c.get("profilo_linkedin") or ""

    elif profilo_ricerca_id:
        # Caso 2: profilo già in profili_ricerca ma non ancora in pipeline
        pr = db.execute(
            "SELECT * FROM profili_ricerca WHERE id = ?", (profilo_ricerca_id,)
        ).fetchone()
        if not pr:
            db.close()
            return jsonify({"errore": "Profilo non trovato"}), 404
        testo_profilo = pr["testo_profilo"] or ""
        nome          = pr["nome"] or ""
        cognome       = pr["cognome"] or ""
        ruolo         = pr["ruolo"] or ""
        azienda       = pr["azienda"] or ""
        linkedin      = pr["linkedin_url"] or ""
        ricerca_id    = pr["ricerca_id"]

    else:
        # Caso 3: dati testuali diretti (ricerca.html, vecchio flusso)
        testo_profilo = dati.get("testo_profilo", "").strip()
        nome    = dati.get("nome", "").strip()
        cognome = dati.get("cognome", "").strip()
        ruolo   = dati.get("ruolo", "").strip()
        azienda = dati.get("azienda", "").strip()
        linkedin = dati.get("linkedin", "").strip()

    if not testo_profilo:
        db.close()
        return jsonify({"errore": "Testo profilo mancante"}), 400

    if risultato_precomputed:
        # Risultato già calcolato dal frontend tramite SSE streaming — salta la chiamata AI
        risultato = risultato_precomputed
    else:
        # Carica impostazioni per il tipo profilo selezionato
        imp_row = db.execute(
            "SELECT * FROM impostazioni_profilo WHERE profilo = ?", (tipo_profilo,)
        ).fetchone()
        imp = dict(imp_row) if imp_row else None

        try:
            risultato = analizza_profilo_linkedin(testo_profilo, tipo_profilo, imp)
        except Exception as e:
            db.close()
            return jsonify({"errore": str(e)}), 500

    # Coercion tipi: ogni valore dal risultato AI deve essere del tipo giusto per PostgreSQL
    def _s(v, fallback=None):
        """Stringa o None."""
        if v in (None, "", {}, []):
            return fallback
        return str(v) if not isinstance(v, str) else (v or fallback)
    def _i(v):
        """Intero o None."""
        try: return int(v) if v not in (None, "", {}, []) else None
        except (TypeError, ValueError): return None

    punteggio     = _i(risultato.get("punteggio"))
    nome_contatto = _s(risultato.get("nome_contatto"), f"{nome} {cognome}".strip() or None)
    ruolo_ai      = _s(risultato.get("ruolo_attuale"), ruolo or None)
    azienda_ai    = _s(risultato.get("azienda"), azienda or None)
    spunti_raw    = risultato.get("spunti_contatto", [])
    spunti_json   = json.dumps(spunti_raw if isinstance(spunti_raw, list) else [], ensure_ascii=False)
    analisi_str   = _s(risultato.get("analisi_percorso"), "") or ""
    messaggio_str = _s(risultato.get("messaggio_outreach"), "") or ""
    anteprima     = testo_profilo[:120].replace("\n", " ").strip()

    e_candidato_nuovo = not candidato_id  # True se stiamo creando un nuovo record

    # Deduplicazione: blocca inserimento se il profilo è già in pipeline
    if e_candidato_nuovo:
        profilo_check = {
            "nome": nome, "cognome": cognome,
            "azienda": azienda_ai or azienda,
            "ruolo": ruolo_ai or ruolo,
            "linkedin": linkedin,
        }
        dup, motivo_dup, cand_id_esistente = is_duplicate(db, profilo_check)
        if dup:
            db.close()
            return jsonify({
                "duplicato": True,
                "motivo": motivo_dup,
                "candidato_id": cand_id_esistente,
            }), 409

    if candidato_id:
        # Aggiorna candidato esistente con analisi e stato pipeline
        db.execute(
            """UPDATE candidati SET
               punteggio=?, analisi=?, spunti=?, messaggio_outreach=?,
               stato='Da contattare', data_aggiornamento=CURRENT_TIMESTAMP
               WHERE id=?""",
            (punteggio, analisi_str, spunti_json, messaggio_str, candidato_id)
        )
    else:
        # Crea nuovo candidato direttamente in pipeline come "Da contattare"
        _gestore = "Salvatore Sabia" if tipo_profilo == "A" else ("Firdaous Filahi" if tipo_profilo == "B" else "Non assegnato")
        cur = db.execute(
            """INSERT INTO candidati
               (nome, cognome, ruolo_attuale, azienda, profilo_linkedin,
                tipo_profilo, stato, punteggio, analisi, spunti,
                messaggio_outreach, ricerca_id, gestore)
               VALUES (?, ?, ?, ?, ?, ?, 'Da contattare', ?, ?, ?, ?, ?, ?)""",
            (nome, cognome, ruolo_ai, azienda_ai, linkedin,
             tipo_profilo, punteggio, analisi_str,
             spunti_json, messaggio_str, ricerca_id, _gestore)
        )
        candidato_id = cur.lastrowid

    # Salva dati arricchiti se presenti (analisi SSE con EnrichLayer)
    if dati_arricchiti_json and candidato_id:
        db.execute(
            "UPDATE candidati SET dati_arricchiti = ? WHERE id = ?",
            (dati_arricchiti_json, candidato_id)
        )

    # Fonte nella cronologia: "ricerca_[id]" se viene da una ricerca, "ricerca_manuale" altrimenti
    fonte = f"ricerca_{ricerca_id}" if ricerca_id else "ricerca_manuale"

    # Salva sempre nella cronologia valutazioni
    db.execute(
        """INSERT INTO valutazioni
           (nome_contatto, ruolo_attuale, azienda, tipo_profilo,
            anteprima_testo, punteggio, analisi, spunti, messaggio_outreach,
            candidato_id, fonte)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (nome_contatto, ruolo_ai, azienda_ai, tipo_profilo, anteprima,
         punteggio, analisi_str, spunti_json, messaggio_str, candidato_id, fonte)
    )

    # Incrementa profili_importati solo per nuovi candidati, non per ri-analisi
    if ricerca_id and e_candidato_nuovo:
        db.execute(
            """UPDATE ricerche_automatiche
               SET profili_importati = COALESCE(profili_importati, 0) + 1
               WHERE id = ?""",
            (ricerca_id,)
        )

    # Collega profili_ricerca al candidato appena creato/aggiornato
    if profilo_ricerca_id:
        db.execute(
            "UPDATE profili_ricerca SET candidato_id = ? WHERE id = ?",
            (candidato_id, profilo_ricerca_id)
        )
    elif ricerca_id and e_candidato_nuovo:
        # Per nuovi candidati senza profilo_ricerca_id, cerca per corrispondenza nome+ricerca
        db.execute(
            """UPDATE profili_ricerca SET candidato_id = ?
               WHERE ricerca_id = ? AND candidato_id IS NULL
               AND nome = ? AND cognome = ?""",
            (candidato_id, ricerca_id, nome, cognome)
        )

    db.commit()
    db.close()

    return jsonify({**risultato, "candidato_id": candidato_id})


@ricerca_bp.route("/ricerca/dettaglio/<int:ricerca_id>")
def dettaglio_ricerca(ricerca_id):
    """
    Pagina di dettaglio di una ricerca.
    Mostra tutti i profili trovati (da profili_ricerca) con i dati di analisi e pipeline.
    Fallback sui candidati diretti per ricerche precedenti all'introduzione di profili_ricerca.
    """
    db = get_db()
    ricerca = db.execute(
        "SELECT * FROM ricerche_automatiche WHERE id = ?", (ricerca_id,)
    ).fetchone()

    if not ricerca:
        db.close()
        return jsonify({"errore": "Ricerca non trovata"}), 404

    # Prima tenta con profili_ricerca (ricerche nuove)
    profili = db.execute(
        """SELECT pr.id           AS profilo_id,
                  pr.nome,        pr.cognome,
                  pr.ruolo,       pr.azienda,
                  pr.location,    pr.linkedin_url,
                  pr.testo_profilo,
                  pr.candidato_id,
                  c.punteggio,    c.analisi,
                  c.spunti,       c.messaggio_outreach,
                  c.stato
           FROM profili_ricerca pr
           LEFT JOIN candidati c ON pr.candidato_id = c.id
           WHERE pr.ricerca_id = ?
           ORDER BY c.punteggio DESC NULLS LAST, pr.id""",
        (ricerca_id,)
    ).fetchall()

    usa_fallback = (len(profili) == 0)

    if usa_fallback:
        # Fallback: ricerche precedenti che hanno candidati ma non profili_ricerca
        profili = db.execute(
            """SELECT id            AS profilo_id,
                      id            AS candidato_id,
                      nome,         cognome,
                      ruolo_attuale AS ruolo,
                      azienda,
                      NULL          AS location,
                      profilo_linkedin AS linkedin_url,
                      NULL          AS testo_profilo,
                      punteggio,    analisi,
                      spunti,       messaggio_outreach,
                      stato
               FROM candidati
               WHERE ricerca_id = ?
               ORDER BY punteggio DESC NULLS LAST""",
            (ricerca_id,)
        ).fetchall()

    db.close()

    ricerca = dict(ricerca)
    profili  = [dict(p) for p in profili]

    try:
        parametri = json.loads(ricerca.get('parametri') or '{}')
    except Exception:
        parametri = {}

    return render_template("ricerca_dettaglio.html",
                           ricerca=ricerca,
                           profili=profili,
                           parametri=parametri,
                           usa_fallback=usa_fallback)


@ricerca_bp.route("/ricerca/dettaglio/<int:ricerca_id>/export_csv")
def export_csv_candidati(ricerca_id):
    """Esporta in CSV tutti i profili di una ricerca specifica."""
    db = get_db()
    ricerca = db.execute(
        "SELECT tipo_profilo, data_ricerca FROM ricerche_automatiche WHERE id = ?",
        (ricerca_id,)
    ).fetchone()
    # Prova prima profili_ricerca, fallback su candidati
    righe = db.execute(
        """SELECT pr.nome, pr.cognome, pr.ruolo, pr.azienda,
                  c.punteggio, c.stato, c.data_aggiornamento
           FROM profili_ricerca pr
           LEFT JOIN candidati c ON pr.candidato_id = c.id
           WHERE pr.ricerca_id = ?
           ORDER BY c.punteggio DESC NULLS LAST, pr.id""",
        (ricerca_id,)
    ).fetchall()
    if not righe:
        righe = db.execute(
            """SELECT nome, cognome, ruolo_attuale AS ruolo, azienda,
                      punteggio, stato, data_aggiornamento
               FROM candidati WHERE ricerca_id = ? ORDER BY punteggio DESC NULLS LAST""",
            (ricerca_id,)
        ).fetchall()
    db.close()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(['Nome', 'Cognome', 'Ruolo', 'Azienda', 'Punteggio', 'Stato', 'Data Valutazione'])
    for c in righe:
        writer.writerow([
            c['nome'], c['cognome'],
            c.get('ruolo') or '',
            c['azienda'] or '',
            c['punteggio'] or '',
            c['stato'] or '',
            c['data_aggiornamento'] or '',
        ])

    tipo = ricerca['tipo_profilo'] if ricerca else 'X'
    return Response(
        '\ufeff' + output.getvalue(),
        mimetype='text/csv; charset=utf-8',
        headers={'Content-Disposition': f'attachment; filename=candidati_ricerca_{ricerca_id}_profilo{tipo}.csv'}
    )


@ricerca_bp.route("/ricerca/importa", methods=["POST"])
def importa():
    """Salva un candidato trovato nella ricerca nel database."""
    dati = request.get_json()
    nome         = dati.get("nome", "").strip()
    cognome      = dati.get("cognome", "").strip()
    ruolo_attuale = dati.get("ruolo", "").strip()
    azienda      = dati.get("azienda", "").strip()
    linkedin     = dati.get("linkedin", "").strip()
    tipo_profilo = dati.get("tipo_profilo", "A")
    note         = dati.get("headline", "").strip()

    if not nome and not cognome:
        return jsonify({"errore": "Nome o cognome mancante"}), 400

    _gestore = "Salvatore Sabia" if tipo_profilo == "A" else ("Firdaous Filahi" if tipo_profilo == "B" else "Non assegnato")
    db = get_db()
    cur = db.execute(
        """INSERT INTO candidati
           (nome, cognome, ruolo_attuale, azienda, profilo_linkedin, tipo_profilo, note, gestore)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (nome, cognome, ruolo_attuale, azienda, linkedin, tipo_profilo, note, _gestore),
    )
    db.commit()
    nuovo_id = cur.lastrowid
    db.close()

    return jsonify({"successo": True, "candidato_id": nuovo_id})
