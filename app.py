"""
Recruiter Assistant — Applicazione principale Flask.
Entry point dell'app, registra tutti i blueprint e inizializza il database.
"""

import os
import time
from flask import Flask, redirect, url_for, request, jsonify
from ai_helpers import test_connessione_api, CLAUDE_MODEL
from dotenv import load_dotenv
from database import init_db, get_db
from routes.auth import auth_bp, login_required
from routes.valutazione import valutazione_bp
from routes.candidati import candidati_bp
from routes.pipeline import pipeline_bp
from routes.contenuti import contenuti_bp
from routes.ricerca import ricerca_bp
from routes.impostazioni import impostazioni_bp
from routes.calendario import calendario_bp
from routes.dashboard import dashboard_bp
from routes.albo import albo_bp

# Carica le variabili d'ambiente dal file .env (se presente)
load_dotenv()

# Crea l'applicazione Flask
app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "recruiter-assistant-secret-2024")

# Registra i blueprint
app.register_blueprint(auth_bp)
app.register_blueprint(valutazione_bp)
app.register_blueprint(candidati_bp)
app.register_blueprint(pipeline_bp)
app.register_blueprint(contenuti_bp)
app.register_blueprint(ricerca_bp)
app.register_blueprint(impostazioni_bp)
app.register_blueprint(calendario_bp)
app.register_blueprint(dashboard_bp)
app.register_blueprint(albo_bp)

# Proteggi tutte le view con login_required (eccetto auth)
for bp in [valutazione_bp, candidati_bp, pipeline_bp, contenuti_bp, ricerca_bp, impostazioni_bp, calendario_bp, dashboard_bp, albo_bp]:
    for endpoint, view_func in app.view_functions.items():
        if endpoint.startswith(bp.name + "."):
            app.view_functions[endpoint] = login_required(view_func)


_STATIC_VERSION = str(int(time.time()))

@app.context_processor
def inject_static_version():
    """Inietta static_version in tutti i template per evitare cache browser sui file statici."""
    return {"static_version": _STATIC_VERSION}


@app.after_request
def add_cache_headers(response):
    """Aggiunge cache headers ai file statici per ridurre le richieste al server."""
    from flask import request as flask_req
    if flask_req.path.startswith("/static/"):
        # 1 anno per file statici (CSS, JS, immagini): cambiano raramente
        response.cache_control.max_age = 31536000
        response.cache_control.public = True
    return response


@app.route("/")
@login_required
def home():
    """Reindirizza alla dashboard principale."""
    return redirect(url_for("dashboard.index"))


# Inizializza il database all'avvio dell'app
with app.app_context():
    try:
        init_db()
    except Exception as e:
        import logging
        logging.getLogger(__name__).error("ERRORE INIT DB: %s", e, exc_info=True)
        print(f"ERRORE INIT DB: {e}")


@app.route("/admin/init-db")
def admin_init_db():
    """
    Esegue init_db() manualmente e crea tutte le tabelle mancanti.
    Sicuro da chiamare più volte (usa IF NOT EXISTS ovunque).
    Utile dopo aver aggiunto il database PostgreSQL su Railway.
    """
    try:
        init_db()
        # Verifica quali tabelle esistono dopo l'inizializzazione
        db = get_db()
        tabelle = db.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'public' ORDER BY table_name"
        ).fetchall()
        db.close()
        nomi = [t["table_name"] for t in tabelle]
        return jsonify({
            "successo": True,
            "messaggio": "Database inizializzato correttamente.",
            "tabelle_create": nomi
        })
    except Exception as e:
        return jsonify({
            "successo": False,
            "errore": str(e)
        }), 500


@app.errorhandler(Exception)
def handle_exception(e):
    """
    Restituisce sempre JSON invece di HTML in caso di errore non gestito.
    Evita il SyntaxError di Safari quando il browser si aspetta JSON ma riceve HTML.
    """
    import traceback, logging
    logging.getLogger(__name__).error("Unhandled exception: %s", traceback.format_exc())
    from flask import request as flask_request
    # Restituisce JSON per tutte le chiamate AJAX/fetch:
    # - Content-Type: application/json (POST con corpo JSON)
    # - X-Requested-With: XMLHttpRequest (jQuery legacy)
    # - Accept: application/json
    # - Metodi non-GET senza HTML nel Accept (DELETE, PUT, PATCH)
    is_ajax = (
        flask_request.is_json
        or flask_request.headers.get("X-Requested-With") == "XMLHttpRequest"
        or "application/json" in flask_request.headers.get("Accept", "")
        or flask_request.method in ("DELETE", "PUT", "PATCH")
    )
    if is_ajax:
        return jsonify({"errore": str(e), "tipo": type(e).__name__}), 500
    # Per pagine HTML rilancia l'eccezione normale di Flask
    raise e


@app.route("/test/proxycurl")
def test_proxycurl():
    """Mostra il JSON grezzo restituito da EnrichLayer per un URL LinkedIn.
    Uso: GET /test/proxycurl?url=https://www.linkedin.com/in/username
    """
    from proxycurl_helpers import arricchisci_profilo
    url = request.args.get("url", "").strip()
    if not url:
        return jsonify({"errore": "Parametro 'url' mancante. Es: /test/proxycurl?url=https://www.linkedin.com/in/username"}), 400
    dati = arricchisci_profilo(url)
    if dati is None:
        return jsonify({"errore": "EnrichLayer non ha restituito dati. Controlla PROXYCURL_API_KEY e che l'URL sia valido."}), 500
    return jsonify(dati)


@app.route("/debug/enrich/<int:candidato_id>")
def debug_enrich(candidato_id):
    """
    Diagnostica completa dell'arricchimento per un candidato.
    Mostra ogni step: URL estratto, EnrichLayer, Claude arricchito.
    Uso: GET /debug/enrich/123
    """
    import re as _re
    from proxycurl_helpers import arricchisci_profilo, is_cache_valida, estrai_testo_proxycurl
    from ai_helpers import analizza_profilo_arricchito

    db = get_db()
    row = db.execute("SELECT * FROM candidati WHERE id = %s" if False else
                     "SELECT id, nome, cognome, profilo_linkedin, dati_proxycurl, dati_arricchiti, punteggio FROM candidati WHERE id = ?",
                     (candidato_id,)).fetchone()
    db.close()

    if not row:
        return jsonify({"errore": f"Candidato {candidato_id} non trovato"}), 404

    out = {
        "candidato_id": candidato_id,
        "nome": f"{row.get('nome')} {row.get('cognome')}",
        "punteggio_db": row.get("punteggio"),
        "dati_arricchiti_in_db": row.get("dati_arricchiti") is not None,
    }

    # Step 1: estrai URL LinkedIn
    url_re = _re.compile(r"https?://(?:www\.)?linkedin\.com/in/[\w\-]+/?")
    lurl = row.get("profilo_linkedin") or ""
    m = url_re.search(lurl)
    linkedin_url = m.group(0) if m else None
    out["linkedin_url_trovato"] = linkedin_url

    if not linkedin_url:
        out["stop"] = "Nessun URL LinkedIn nel campo profilo_linkedin del candidato"
        return jsonify(out)

    punteggio = row.get("punteggio") or 0
    if punteggio < 6:
        out["stop"] = f"Punteggio {punteggio} < 6: soglia arricchimento non raggiunta"
        return jsonify(out)

    # Step 2: controlla cache
    raw_prx = row.get("dati_proxycurl")
    dati_prx = None
    if raw_prx:
        try:
            cached = __import__("json").loads(raw_prx)
            if is_cache_valida(cached):
                dati_prx = cached
                out["enrichlayer"] = "cache valida usata"
        except Exception:
            pass

    # Step 3: chiama EnrichLayer se non c'è cache
    if not dati_prx:
        dati_prx = arricchisci_profilo(linkedin_url)
        if dati_prx is None:
            out["enrichlayer"] = "FAIL: EnrichLayer ha restituito None (chiave errata o URL non trovato)"
            out["stop"] = "EnrichLayer fallito"
            return jsonify(out)
        out["enrichlayer"] = f"OK: {len(dati_prx)} campi restituiti"

    # Step 4: testo estratto da Proxycurl
    testo_prx = estrai_testo_proxycurl(dati_prx)
    out["proxycurl_testo_estratto"] = testo_prx[:300] if testo_prx else "(vuoto)"

    # Step 5: chiama Claude arricchito
    dati_base = {
        "punteggio": punteggio,
        "analisi_percorso": None,
        "ruolo_attuale": row.get("ruolo_attuale") if hasattr(row, "get") else None,
        "azienda": row.get("azienda") if hasattr(row, "get") else None,
        "anni_esperienza": None,
    }
    try:
        enriched = analizza_profilo_arricchito(lurl[:500], "A", dati_prx, dati_base)
        if enriched:
            out["claude_arricchito"] = f"OK: {list(enriched.keys())}"
            out["risultato"] = enriched
        else:
            out["claude_arricchito"] = "FAIL: restituito dict vuoto (JSON parse error o eccezione silenziosa)"
    except Exception as e:
        out["claude_arricchito"] = f"ECCEZIONE: {e}"

    return jsonify(out)


@app.route("/admin/test-api")
def admin_test_api():
    """Verifica la connessione all'API Anthropic con una chiamata minimale."""
    risultato = test_connessione_api()
    risultato["model_usato"] = CLAUDE_MODEL
    return jsonify(risultato), 200 if risultato["ok"] else 500


if __name__ == "__main__":
    # Avvia il server in modalità debug per lo sviluppo
    app.run(debug=True, port=5001)
