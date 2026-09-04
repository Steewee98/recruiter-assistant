"""
Blueprint Albo OCF — ricerca sulla popolazione ufficiale dei consulenti finanziari
e radar dei passaggi di rete.

Differenza rispetto a /ricerca (LinkedIn via Apify):
  • /ricerca      → campiona LinkedIn, costa, restituisce ~20 profili per volta,
                    e non sa se la persona è davvero un consulente iscritto.
  • /albo         → interroga l'elenco ufficiale OCF già scaricato in database:
                    istantaneo, gratuito, completo per rete e per città, e ogni
                    riga è per definizione un CF abilitato.

L'importazione in pipeline da qui NON passa dall'AI: sono dati anagrafici
verificati dall'Organismo, non serve un giudizio del modello per salvarli
(l'analisi resta disponibile dopo, sul singolo candidato).
"""

import logging
import threading

from flask import Blueprint, jsonify, render_template, request

from database import get_db
from dedup import is_duplicate
from routes.auth import login_required
from services import albo_ocf

log = logging.getLogger(__name__)

albo_bp = Blueprint("albo", __name__)

# Stato della sincronizzazione in corso. Gunicorn gira con 1 worker e 4 thread
# (vedi gunicorn.conf.py), quindi un dizionario di modulo è sufficiente e non
# richiede un job persistente.
_sync_stato = {"in_corso": False, "esito": None}
_sync_lock = threading.Lock()


@albo_bp.route("/albo")
@login_required
def index():
    stats = albo_ocf.statistiche()
    ultimi_movimenti = albo_ocf.movimenti(tipo="cambio_rete", giorni=365, limite=50)
    return render_template("albo.html", stats=stats, movimenti=ultimi_movimenti,
                           sync_in_corso=_sync_stato["in_corso"])


@albo_bp.route("/albo/sincronizza", methods=["POST"])
@login_required
def sincronizza():
    """
    Avvia lo scaricamento dell'elenco ufficiale in background e risponde subito:
    56.000 righe richiedono più del tempo di una richiesta HTTP.
    """
    elenco = (request.get_json() or {}).get("elenco", "abilitati")
    with _sync_lock:
        if _sync_stato["in_corso"]:
            return jsonify({"ok": False, "errore": "Una sincronizzazione è già in corso."}), 409
        _sync_stato["in_corso"] = True
        _sync_stato["esito"] = None

    def _lavora():
        try:
            esito = albo_ocf.sincronizza(elenco)
        except Exception as e:  # pragma: no cover — la sync già cattura da sé
            log.error("Sync albo fallita: %s", e, exc_info=True)
            esito = {"ok": False, "errore": str(e)}
        with _sync_lock:
            _sync_stato["esito"] = esito
            _sync_stato["in_corso"] = False

    threading.Thread(target=_lavora, daemon=True).start()
    return jsonify({"ok": True, "avviata": True})


@albo_bp.route("/albo/stato-sync")
@login_required
def stato_sync():
    with _sync_lock:
        return jsonify({"in_corso": _sync_stato["in_corso"], "esito": _sync_stato["esito"]})


@albo_bp.route("/albo/cerca", methods=["POST"])
@login_required
def cerca():
    """
    Ricerca nell'albo. Nessuna chiamata esterna: è una query locale.
    Segnala quali risultati sono già in pipeline invece di nasconderli:
    su una fonte completa "già visto" è un'informazione, non uno scarto.
    """
    d = request.get_json() or {}
    try:
        ris = albo_ocf.cerca(
            rete=d.get("rete", ""), comune=d.get("comune", ""),
            provincia=d.get("provincia", ""), regione=d.get("regione", ""),
            eta_min=d.get("eta_min") or None, eta_max=d.get("eta_max") or None,
            limite=min(int(d.get("limite") or 100), 500),
            offset=int(d.get("offset") or 0),
        )
    except Exception as e:
        log.error("Ricerca albo fallita: %s", e, exc_info=True)
        return jsonify({"errore": f"Ricerca nell'albo non riuscita: {e}"}), 500

    db = get_db()
    try:
        for p in ris["profili"]:
            dup, motivo, cid = is_duplicate(db, {
                "nome": p["nome"], "cognome": p["cognome"], "azienda": p.get("rete", ""),
            })
            p["gia_in_pipeline"] = bool(dup)
            p["motivo_dup"] = motivo if dup else ""
            p["candidato_id"] = cid
    finally:
        db.close()

    ris["nuovi"] = sum(1 for p in ris["profili"] if not p["gia_in_pipeline"])
    return jsonify(ris)


@albo_bp.route("/albo/importa", methods=["POST"])
@login_required
def importa():
    """
    Porta in pipeline i profili scelti dall'albo. Senza AI e senza Apify:
    nome, cognome, rete e città bastano per iniziare a lavorare il contatto.
    """
    profili = (request.get_json() or {}).get("profili") or []
    if not profili:
        return jsonify({"errore": "Nessun profilo selezionato."}), 400

    db = get_db()
    inseriti, saltati = 0, 0
    try:
        for p in profili:
            nome = (p.get("nome") or "").strip()
            cognome = (p.get("cognome") or "").strip()
            rete = (p.get("rete") or "").strip()
            if not cognome:
                saltati += 1
                continue
            dup, _motivo, _cid = is_duplicate(db, {"nome": nome, "cognome": cognome, "azienda": rete})
            if dup:
                saltati += 1
                continue
            citta = ", ".join(x for x in [p.get("comune", ""), p.get("provincia", "")] if x)
            note = f"Da albo OCF · {citta}"
            if p.get("eta"):
                note += f" · {p['eta']} anni"
            if p.get("n_cambi"):
                note += f" · {p['n_cambi']} cambio/i di rete rilevati"
            db.execute(
                """INSERT INTO candidati (nome, cognome, ruolo_attuale, azienda, note,
                                          tipo_profilo, stato, source, url_fonte)
                   VALUES (?, ?, ?, ?, ?, ?, ?, 'ocf', '')""",
                (nome, cognome, "Consulente finanziario", rete, note,
                 p.get("tipo_profilo") or "A", "Da valutare"),
            )
            inseriti += 1
        db.commit()
    except Exception as e:
        log.error("Import da albo fallito: %s", e, exc_info=True)
        return jsonify({"errore": f"Import non riuscito: {e}"}), 500
    finally:
        db.close()

    return jsonify({"ok": True, "inseriti": inseriti, "saltati": saltati})


@albo_bp.route("/albo/movimenti")
@login_required
def lista_movimenti():
    return jsonify({"movimenti": albo_ocf.movimenti(
        tipo=request.args.get("tipo", ""),
        giorni=int(request.args.get("giorni", 365)),
        rete=request.args.get("rete", ""),
        provincia=request.args.get("provincia", ""),
    )})
