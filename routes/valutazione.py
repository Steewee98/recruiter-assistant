"""
Modulo 1 — Valutazione Candidati.
Gestisce l'analisi di profili LinkedIn tramite Claude AI.
"""

import io
import csv
import json
import os
import re
import requests
from flask import Blueprint, render_template, request, jsonify, redirect, url_for, Response
from ai_helpers import analizza_profilo_linkedin, rigenera_messaggio_outreach, analizza_profilo_linkedin_stream
from database import get_db

APIFY_BASE  = "https://api.apify.com/v2"
APIFY_CERCA_NOME = "harvestapi~linkedin-profile-search-by-name"

# Blueprint per il modulo valutazione
valutazione_bp = Blueprint("valutazione", __name__)


@valutazione_bp.route("/valutazione")
def index():
    """Redirect alla pipeline con tab valutazione."""
    params = {k: v for k, v in request.args.items()}
    params['tab'] = 'valutazione'
    return redirect(url_for('pipeline.index', **params))


@valutazione_bp.route("/valutazione/analizza", methods=["POST"])
def analizza():
    """Endpoint AJAX per analizzare un profilo LinkedIn."""
    dati = request.get_json()
    testo_profilo = dati.get("testo_profilo", "").strip()
    tipo_profilo = dati.get("tipo_profilo", "A")
    candidato_id = dati.get("candidato_id")

    if not testo_profilo:
        return jsonify({"errore": "Inserire il testo del profilo LinkedIn"}), 400

    # Chiama Claude per l'analisi
    risultato = analizza_profilo_linkedin(testo_profilo, tipo_profilo)

    spunti_json = json.dumps(risultato["spunti_contatto"], ensure_ascii=False)
    # Anteprima: prime 120 caratteri del testo profilo
    anteprima = testo_profilo[:120].replace("\n", " ").strip()

    db = get_db()

    # Dati estratti da Claude — forzati al tipo corretto per PostgreSQL
    def _s(v):  # stringa o None
        return str(v) if v not in (None, "", {}, []) else None
    def _i(v):  # intero o None
        try: return int(v) if v not in (None, "", {}, []) else None
        except (TypeError, ValueError): return None

    nome_contatto   = _s(risultato.get("nome_contatto"))
    ruolo_attuale   = _s(risultato.get("ruolo_attuale"))
    azienda         = _s(risultato.get("azienda"))
    anni_esperienza = _i(risultato.get("anni_esperienza"))

    # Fallback nome dal DB se arriva da candidato registrato
    if not nome_contatto and candidato_id:
        row = db.execute(
            "SELECT nome, cognome FROM candidati WHERE id = ?", (candidato_id,)
        ).fetchone()
        if row:
            nome_contatto = f"{row['nome']} {row['cognome']}"

    # Salva sempre nella cronologia valutazioni con fonte 'manuale'
    db.execute(
        """INSERT INTO valutazioni
           (nome_contatto, ruolo_attuale, azienda, anni_esperienza,
            tipo_profilo, anteprima_testo, punteggio, analisi, spunti, messaggio_outreach, candidato_id, fonte)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            nome_contatto, ruolo_attuale, azienda, anni_esperienza,
            tipo_profilo, anteprima,
            risultato["punteggio"],
            risultato["analisi_percorso"],
            spunti_json,
            risultato["messaggio_outreach"],
            candidato_id or None,
            'manuale',
        ),
    )

    # Se c'è un candidato_id, aggiorna anche il record candidato
    if candidato_id:
        db.execute(
            """UPDATE candidati SET
               punteggio = ?,
               analisi = ?,
               spunti = ?,
               messaggio_outreach = ?,
               profilo_linkedin = ?,
               tipo_profilo = ?,
               data_aggiornamento = CURRENT_TIMESTAMP
               WHERE id = ?""",
            (
                risultato["punteggio"],
                risultato["analisi_percorso"],
                spunti_json,
                risultato["messaggio_outreach"],
                testo_profilo,
                tipo_profilo,
                candidato_id,
            ),
        )

    db.commit()
    db.close()

    return jsonify(risultato)


@valutazione_bp.route("/valutazione/analizza_stream", methods=["POST"])
def analizza_stream():
    """
    Endpoint SSE: streama l'analisi profilo LinkedIn chunk per chunk.
    Se punteggio >= 6 e c'è un URL LinkedIn, arricchisce con Proxycurl.
    """
    import sys
    print(f"=== ROUTE HIT: {request.path} ===", flush=True)
    dati              = request.get_json()
    testo_profilo     = dati.get("testo_profilo", "").strip()
    tipo_profilo      = dati.get("tipo_profilo", "A")
    candidato_id      = dati.get("candidato_id")
    punteggio_forzato = dati.get("punteggio_forzato")
    if punteggio_forzato is not None:
        try:
            punteggio_forzato = int(punteggio_forzato)
        except (TypeError, ValueError):
            punteggio_forzato = None
    print(f"=== STREAM PARAMS: tipo={tipo_profilo} candidato_id={candidato_id} linkedin_url={dati.get('linkedin_url')} punteggio_forzato={punteggio_forzato} testo_len={len(testo_profilo)} ===", flush=True)

    if not testo_profilo:
        def _err():
            yield f"data: {json.dumps({'type': 'errore', 'messaggio': 'Inserire il testo del profilo LinkedIn'})}\n\n"
        return Response(_err(), mimetype="text/event-stream")

    # URL LinkedIn passato esplicitamente dal frontend (priorità massima)
    linkedin_url_esplicito = (dati.get("linkedin_url") or "").strip() or None

    # Recupera linkedin_url e dati_proxycurl cached dal DB (se candidato noto)
    linkedin_url           = linkedin_url_esplicito
    dati_proxycurl_cached  = None

    _url_re = re.compile(r"https?://(?:www\.)?linkedin\.com/in/[\w\-]+/?")

    if candidato_id:
        try:
            db = get_db()
            row = db.execute(
                "SELECT profilo_linkedin, dati_proxycurl FROM candidati WHERE id = ?",
                (candidato_id,),
            ).fetchone()
            db.close()
            if row:
                # Usa URL dal DB solo se non già fornito esplicitamente dal frontend
                if not linkedin_url:
                    lurl = row.get("profilo_linkedin") or ""
                    m = _url_re.search(lurl)
                    if m:
                        linkedin_url = m.group(0)
                raw_prx = row.get("dati_proxycurl")
                if raw_prx:
                    try:
                        dati_proxycurl_cached = json.loads(raw_prx)
                    except Exception:
                        pass
        except Exception:
            pass

    # Fallback: estrai URL LinkedIn dal testo profilo
    if not linkedin_url:
        m = _url_re.search(testo_profilo)
        if m:
            linkedin_url = m.group(0)

    def _genera():
        for chunk in analizza_profilo_linkedin_stream(
            testo_profilo, tipo_profilo,
            linkedin_url=linkedin_url,
            dati_proxycurl_cached=dati_proxycurl_cached,
            punteggio_forzato=punteggio_forzato,
        ):
            # Intercetta l'evento interno _proxycurl_data: salva in DB, non inviare al browser
            if '"_proxycurl_data"' in chunk:
                try:
                    ev = json.loads(chunk[chunk.index('data:') + 5:].strip())
                    if ev.get("type") == "_proxycurl_data" and candidato_id:
                        dati_prx_json = json.dumps(ev["dati"], ensure_ascii=False)
                        db2 = get_db()
                        db2.execute(
                            "UPDATE candidati SET dati_proxycurl=? WHERE id=?",
                            (dati_prx_json, candidato_id),
                        )
                        db2.commit()
                        db2.close()
                except Exception as e_save:
                    import logging
                    logging.getLogger(__name__).error("[stream] salvataggio proxycurl fallito: %s", e_save)
                continue  # non inoltrare al browser
            yield chunk

    return Response(
        _genera(),
        mimetype="text/event-stream",
        headers={
            "Cache-Control":    "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@valutazione_bp.route("/valutazione/salva_analisi", methods=["POST"])
def salva_analisi():
    """
    Salva il risultato di un'analisi nel DB (chiamato dal frontend dopo lo stream).
    Accetta lo stesso payload di /analizza, più il campo 'risultato' già parsato.
    """
    dati          = request.get_json()
    testo_profilo = dati.get("testo_profilo", "").strip()
    tipo_profilo  = dati.get("tipo_profilo", "A")
    candidato_id  = dati.get("candidato_id")
    risultato     = dati.get("risultato", {})

    if not risultato:
        return jsonify({"errore": "Risultato mancante"}), 400

    def _s(v):
        return str(v) if v not in (None, "", {}, []) else None
    def _i(v):
        try: return int(v) if v not in (None, "", {}, []) else None
        except (TypeError, ValueError): return None

    nome_contatto   = _s(risultato.get("nome_contatto"))
    ruolo_attuale   = _s(risultato.get("ruolo_attuale"))
    azienda         = _s(risultato.get("azienda"))
    anni_esperienza = _i(risultato.get("anni_esperienza"))
    spunti_json     = json.dumps(risultato.get("spunti_contatto", []), ensure_ascii=False)
    anteprima       = testo_profilo[:120].replace("\n", " ").strip()

    # Dati arricchiti Proxycurl (se presenti nel risultato)
    arricchito = risultato.get("arricchito", False)
    dati_prx = risultato.get("dati_proxycurl")
    dati_prx_json = json.dumps(dati_prx, ensure_ascii=False) if dati_prx else None

    if arricchito:
        enriched_keys = [
            "punteggio_compatibilita", "indice_mobilita", "punteggio_qualita_profilo",
            "pattern_carriera", "momento_contatto", "motivazione_probabile",
            "segnali_positivi", "segnali_negativi", "rischi",
            "analisi_attivita", "messaggio_personalizzato", "sintesi",
        ]
        dati_arricchiti = {k: risultato.get(k) for k in enriched_keys if risultato.get(k) is not None}
        dati_arricchiti_json = json.dumps(dati_arricchiti, ensure_ascii=False)
    else:
        dati_arricchiti_json = None

    db = get_db()

    if not nome_contatto and candidato_id:
        row = db.execute("SELECT nome, cognome FROM candidati WHERE id = ?", (candidato_id,)).fetchone()
        if row:
            nome_contatto = f"{row['nome']} {row['cognome']}"

    db.execute(
        """INSERT INTO valutazioni
           (nome_contatto, ruolo_attuale, azienda, anni_esperienza,
            tipo_profilo, anteprima_testo, punteggio, analisi, spunti, messaggio_outreach,
            candidato_id, fonte, dati_arricchiti)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (nome_contatto, ruolo_attuale, azienda, anni_esperienza,
         tipo_profilo, anteprima,
         risultato.get("punteggio"),
         risultato.get("analisi_percorso"),
         spunti_json,
         risultato.get("messaggio_outreach"),
         candidato_id or None,
         "manuale",
         dati_arricchiti_json),
    )

    # Fallback: se candidato_id è null, prova a trovare il candidato tramite URL LinkedIn
    if not candidato_id and arricchito and testo_profilo:
        m_url = re.search(r"https?://(?:www\.)?linkedin\.com/in/[\w\-]+/?", testo_profilo)
        if m_url:
            linkedin_url = m_url.group(0)
            row_cand = db.execute(
                "SELECT id FROM candidati WHERE profilo_linkedin LIKE ?",
                (f"%{linkedin_url}%",),
            ).fetchone()
            if row_cand:
                candidato_id = row_cand["id"]

    if candidato_id:
        db.execute(
            """UPDATE candidati SET
               punteggio=?, analisi=?, spunti=?, messaggio_outreach=?,
               tipo_profilo=?,
               dati_proxycurl=COALESCE(?, dati_proxycurl),
               dati_arricchiti=COALESCE(?, dati_arricchiti),
               data_aggiornamento=CURRENT_TIMESTAMP
               WHERE id=?""",
            (risultato.get("punteggio"), risultato.get("analisi_percorso"),
             spunti_json, risultato.get("messaggio_outreach"),
             tipo_profilo, dati_prx_json, dati_arricchiti_json, candidato_id),
        )

    db.commit()
    db.close()
    return jsonify({"successo": True, "candidato_id_usato": candidato_id})


@valutazione_bp.route("/valutazione/cerca_per_nome", methods=["POST"])
def cerca_per_nome():
    """Avvia la ricerca Apify per nome/cognome e restituisce run_id per il polling."""
    dati = request.get_json()
    nome    = dati.get("nome", "").strip()
    cognome = dati.get("cognome", "").strip()

    if not nome and not cognome:
        return jsonify({"errore": "Inserisci almeno nome o cognome"}), 400

    api_key = os.environ.get("APIFY_API_KEY", "")
    if not api_key:
        return jsonify({"errore": "APIFY_API_KEY non configurata nel file .env"}), 500

    run_input = {
        "count": 6,
        "profileScraperMode": "Short",
    }
    if nome:
        run_input["firstName"] = nome
    if cognome:
        run_input["lastName"] = cognome

    try:
        resp = requests.post(
            f"{APIFY_BASE}/acts/{APIFY_CERCA_NOME}/runs",
            json=run_input,
            params={"token": api_key},
            timeout=15,
        )
        resp.raise_for_status()
        run = resp.json().get("data", {})
        return jsonify({
            "run_id": run.get("id"),
            "dataset_id": run.get("defaultDatasetId"),
        })
    except requests.exceptions.HTTPError:
        return jsonify({"errore": f"Errore Apify: {resp.status_code} — {resp.text[:200]}"}), 500
    except requests.exceptions.RequestException as e:
        return jsonify({"errore": f"Errore connessione: {str(e)}"}), 500


@valutazione_bp.route("/valutazione/poll_run/<run_id>")
def poll_run(run_id):
    """Controlla lo stato di una run Apify e, se completata, restituisce i risultati."""
    api_key = os.environ.get("APIFY_API_KEY", "")

    try:
        # Controlla stato run
        r = requests.get(
            f"{APIFY_BASE}/actor-runs/{run_id}",
            params={"token": api_key},
            timeout=10,
        )
        r.raise_for_status()
        run = r.json().get("data", {})
        status = run.get("status", "")

        if status in ("RUNNING", "READY"):
            return jsonify({"status": "running"})

        if status != "SUCCEEDED":
            return jsonify({"errore": f"Run terminata con stato: {status}"}), 500

        # Recupera i risultati dal dataset
        dataset_id = run.get("defaultDatasetId")
        dr = requests.get(
            f"{APIFY_BASE}/datasets/{dataset_id}/items",
            params={"token": api_key},
            timeout=15,
        )
        dr.raise_for_status()
        items = dr.json()

        profili = []
        for p in items:
            if not isinstance(p, dict):
                continue
            nome_completo = p.get("name", "")
            parti = nome_completo.strip().split(" ", 1)
            nome_p    = parti[0] if parti else ""
            cognome_p = parti[1] if len(parti) > 1 else ""
            position  = p.get("position", "")
            linkedin  = p.get("linkedinUrl", "")
            loc_obj   = p.get("location") or {}
            location  = loc_obj.get("linkedinText", "") if isinstance(loc_obj, dict) else str(loc_obj)

            # Testo profilo per Claude
            testo_parts = []
            if nome_completo:
                testo_parts.append(f"Nome: {nome_completo}")
            if position:
                testo_parts.append(f"Posizione attuale: {position}")
            if location:
                testo_parts.append(f"Location: {location}")
            if linkedin:
                testo_parts.append(f"LinkedIn: {linkedin}")

            profili.append({
                "nome":          nome_p,
                "cognome":       cognome_p,
                "headline":      position,
                "location":      location,
                "linkedin":      linkedin,
                "testo_profilo": "\n".join(testo_parts),
            })

        return jsonify({"status": "done", "profili": profili})

    except requests.exceptions.RequestException as e:
        return jsonify({"errore": f"Errore polling: {str(e)}"}), 500


@valutazione_bp.route("/valutazione/export_csv")
def export_csv():
    """Esporta la cronologia valutazioni in formato CSV."""
    db = get_db()
    rows = db.execute(
        "SELECT * FROM valutazioni ORDER BY data_valutazione DESC"
    ).fetchall()
    db.close()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        'ID', 'Data', 'Contatto', 'Ruolo', 'Azienda',
        'Tipo Profilo', 'Punteggio', 'Fonte', 'Anteprima'
    ])
    for r in rows:
        writer.writerow([
            r['id'],
            r['data_valutazione'],
            r['nome_contatto'] or '',
            r['ruolo_attuale'] or '',
            r['azienda'] or '',
            r['tipo_profilo'],
            r['punteggio'],
            r['fonte'] if r['fonte'] else 'manuale',
            r['anteprima_testo'] or '',
        ])

    return Response(
        '\ufeff' + output.getvalue(),  # BOM per compatibilità Excel
        mimetype='text/csv; charset=utf-8',
        headers={'Content-Disposition': 'attachment; filename=cronologia_valutazioni.csv'}
    )


@valutazione_bp.route("/valutazione/rigenera_messaggio", methods=["POST"])
def rigenera_messaggio():
    """Endpoint AJAX per rigenerare o riscrivere il messaggio di outreach."""
    dati = request.get_json()
    testo_profilo = dati.get("testo_profilo", "").strip()
    messaggio_attuale = dati.get("messaggio_attuale", "").strip()
    istruzioni = dati.get("istruzioni", "").strip()

    if not messaggio_attuale:
        return jsonify({"errore": "Nessun messaggio da rielaborare"}), 400

    nuovo_messaggio = rigenera_messaggio_outreach(testo_profilo, messaggio_attuale, istruzioni)
    return jsonify({"messaggio": nuovo_messaggio})
