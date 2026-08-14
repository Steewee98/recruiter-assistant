"""
Modulo 3 — Pipeline Candidati.
Tabella con tutti i candidati, stato avanzamento e azioni disponibili.
Include i tab: Pipeline, Valutazione, Calendario, Cronologia.
"""

import json
from flask import Blueprint, render_template, request, jsonify, redirect, url_for
from database import get_db
from ai_helpers import genera_messaggio_followup, rigenera_messaggio_followup

# Blueprint per il modulo pipeline
pipeline_bp = Blueprint("pipeline", __name__)

# Stati disponibili nel processo di selezione
STATI_VALIDI = ["Da valutare", "Da contattare", "Richiesta Inviata", "Messaggio Inviato", "Chiuso"]

# Gestori disponibili
GESTORI_VALIDI = ["Salvatore Sabia", "Firdaous Filahi", "Non assegnato"]

# Costanti tab Calendario
TIPI_APPUNTAMENTO = ['Chiamata', 'Video call', 'Incontro di persona']
GESTORI_CAL = ['Salvatore Sabia', 'Firdaous Filahi']
STATI_APPUNTAMENTO = ['Da fare', 'Completato', 'Annullato']


@pipeline_bp.route("/pipeline")
def index():
    """Pagina principale della pipeline con tab: Pipeline, Valutazione, Calendario, Cronologia."""
    tab = request.args.get("tab", "pipeline")

    db = get_db()
    candidati = db.execute(
        "SELECT * FROM candidati ORDER BY data_aggiornamento DESC"
    ).fetchall()

    # Converti Row in dizionari per passarli al template
    candidati_lista = [dict(c) for c in candidati]

    # Deserializza gli spunti JSON per ogni candidato
    for c in candidati_lista:
        if c.get("spunti"):
            try:
                c["spunti"] = json.loads(c["spunti"])
            except Exception:
                c["spunti"] = []

    # Prossimo appuntamento per ogni candidato
    prossimi_app = {}
    try:
        righe = db.execute(
            """SELECT candidato_id, MIN(data_ora) as prossimo
               FROM appuntamenti
               WHERE stato = 'Da fare' AND data_ora >= CURRENT_TIMESTAMP
               GROUP BY candidato_id"""
        ).fetchall()
        for r in righe:
            prossimi_app[r['candidato_id']] = r['prossimo']
    except Exception:
        pass

    # Cronologia valutazioni (tab Valutazione + tab Cronologia)
    cronologia = db.execute(
        "SELECT * FROM valutazioni ORDER BY data_valutazione DESC"
    ).fetchall()
    cronologia = [dict(r) for r in cronologia]

    # Appuntamenti (tab Calendario)
    appuntamenti = db.execute("""
        SELECT a.*,
               COALESCE(c.nome || ' ' || c.cognome, 'Candidato rimosso') AS candidato_nome
        FROM appuntamenti a
        LEFT JOIN candidati c ON a.candidato_id = c.id
        ORDER BY a.data_ora ASC
    """).fetchall()
    appuntamenti = [dict(a) for a in appuntamenti]

    # Candidato pre-selezionato per tab Valutazione (param candidato_id)
    candidato_val = None
    candidato_id_param = request.args.get("candidato_id")
    if candidato_id_param:
        row = db.execute(
            "SELECT * FROM candidati WHERE id = ?", (candidato_id_param,)
        ).fetchone()
        if row:
            candidato_val = dict(row)

    db.close()

    return render_template(
        "pipeline.html",
        candidati=candidati_lista,
        stati=STATI_VALIDI,
        gestori=GESTORI_VALIDI,
        prossimi_app=prossimi_app,
        cronologia=cronologia,
        appuntamenti=appuntamenti,
        tipi_app=TIPI_APPUNTAMENTO,
        gestori_cal=GESTORI_CAL,
        stati_app=STATI_APPUNTAMENTO,
        candidato=candidato_val,
        tab_attivo=tab,
    )


@pipeline_bp.route("/pipeline/aggiorna_stato", methods=["POST"])
def aggiorna_stato():
    """Endpoint AJAX per aggiornare lo stato di un candidato."""
    dati = request.get_json()
    candidato_id = dati.get("id")
    nuovo_stato = dati.get("stato")

    if nuovo_stato not in STATI_VALIDI:
        return jsonify({"errore": "Stato non valido"}), 400

    db = get_db()
    db.execute(
        "UPDATE candidati SET stato = ?, data_aggiornamento = CURRENT_TIMESTAMP WHERE id = ?",
        (nuovo_stato, candidato_id),
    )
    db.commit()
    db.close()

    return jsonify({"successo": True, "stato": nuovo_stato})


@pipeline_bp.route("/pipeline/aggiorna_gestore", methods=["POST"])
def aggiorna_gestore():
    """Endpoint AJAX per aggiornare il gestore di un candidato."""
    dati = request.get_json()
    candidato_id = dati.get("id")
    nuovo_gestore = dati.get("gestore")

    if nuovo_gestore not in GESTORI_VALIDI:
        return jsonify({"errore": "Gestore non valido"}), 400

    db = get_db()
    db.execute(
        "UPDATE candidati SET gestore = ?, data_aggiornamento = CURRENT_TIMESTAMP WHERE id = ?",
        (nuovo_gestore, candidato_id),
    )
    db.commit()
    db.close()

    return jsonify({"successo": True, "gestore": nuovo_gestore})


@pipeline_bp.route("/pipeline/followup/<int:candidato_id>", methods=["POST"])
def genera_followup(candidato_id):
    """Endpoint AJAX per generare un messaggio di follow-up con AI."""
    db = get_db()
    row = db.execute(
        "SELECT * FROM candidati WHERE id = ?", (candidato_id,)
    ).fetchone()
    db.close()

    if not row:
        return jsonify({"errore": "Candidato non trovato"}), 404

    candidato = dict(row)
    messaggio = genera_messaggio_followup(candidato)
    return jsonify({"messaggio": messaggio})


@pipeline_bp.route("/pipeline/rigenera_followup/<int:candidato_id>", methods=["POST"])
def rigenera_followup(candidato_id):
    """Endpoint AJAX per rigenerare o riscrivere il follow-up con istruzioni personalizzate."""
    dati = request.get_json()
    messaggio_attuale = dati.get("messaggio_attuale", "").strip()
    istruzioni = dati.get("istruzioni", "").strip()

    db = get_db()
    row = db.execute("SELECT * FROM candidati WHERE id = ?", (candidato_id,)).fetchone()
    db.close()

    if not row:
        return jsonify({"errore": "Candidato non trovato"}), 404

    messaggio = rigenera_messaggio_followup(dict(row), messaggio_attuale, istruzioni)
    return jsonify({"messaggio": messaggio})


@pipeline_bp.route("/pipeline/<int:candidato_id>/note", methods=["PATCH"])
def aggiorna_note(candidato_id):
    """Endpoint AJAX per aggiornare le note di un candidato."""
    dati = request.get_json()
    note = dati.get("note", "")
    db = get_db()
    db.execute(
        "UPDATE candidati SET note = ?, data_aggiornamento = CURRENT_TIMESTAMP WHERE id = ?",
        (note, candidato_id),
    )
    db.commit()
    db.close()
    return jsonify({"successo": True})


@pipeline_bp.route("/pipeline/triangola/<int:candidato_id>", methods=["POST"])
def triangola_candidato(candidato_id):
    """
    Triangola un contatto: incrocia LinkedIn + OCF + riconoscimenti e restituisce
    un dossier ("ci piace più di LinkedIn?" + "come contattarlo?").
    Vedi services/triangolazione.py e docs/piano_multifonte.md.
    """
    db = get_db()
    row = db.execute("SELECT * FROM candidati WHERE id = ?", (candidato_id,)).fetchone()
    db.close()
    if not row:
        return jsonify({"errore": "Candidato non trovato"}), 404

    from services.triangolazione import triangola
    esito = triangola(dict(row))
    if not esito.get("ok"):
        return jsonify({"errore": esito.get("errore", "Triangolazione non riuscita")}), 502

    # Persisti il dossier per non doverlo ricalcolare (risparmio API)
    try:
        db = get_db()
        db.execute(
            "UPDATE candidati SET triangolazione = ?, data_triangolazione = CURRENT_TIMESTAMP WHERE id = ?",
            (json.dumps(esito, ensure_ascii=False), candidato_id),
        )
        db.commit()
        db.close()
    except Exception:
        pass  # il salvataggio è best-effort: il dossier è comunque restituito al client

    return jsonify(esito)


@pipeline_bp.route("/pipeline/elimina/<int:candidato_id>", methods=["DELETE"])
def elimina_candidato(candidato_id):
    """Elimina un candidato dalla pipeline."""
    db = get_db()
    db.execute("DELETE FROM candidati WHERE id = ?", (candidato_id,))
    db.commit()
    db.close()
    return jsonify({"successo": True})
