"""
Modulo Impostazioni — Configurazione dei criteri di valutazione per Profilo A e Profilo B.
I parametri vengono salvati nel DB e letti da ai_helpers.py durante l'analisi.
"""

from flask import Blueprint, render_template, request, jsonify
from database import get_db
import logging

log = logging.getLogger(__name__)

impostazioni_bp = Blueprint("impostazioni", __name__)


def get_impostazioni(profilo):
    """Restituisce le impostazioni per il profilo dato ('A' o 'B'), o None se non configurate."""
    db = get_db()
    row = db.execute(
        "SELECT * FROM impostazioni_profilo WHERE profilo = ?", (profilo,)
    ).fetchone()
    db.close()
    return dict(row) if row else None


@impostazioni_bp.route("/impostazioni")
def index():
    """Pagina impostazioni profili."""
    db = get_db()
    imp_a    = db.execute("SELECT * FROM impostazioni_profilo WHERE profilo = 'A'").fetchone()
    imp_b    = db.execute("SELECT * FROM impostazioni_profilo WHERE profilo = 'B'").fetchone()
    scartati = db.execute(
        "SELECT * FROM profili_scartati ORDER BY data_scarto DESC"
    ).fetchall()
    db.close()
    return render_template(
        "impostazioni.html",
        imp_a=dict(imp_a) if imp_a else {},
        imp_b=dict(imp_b) if imp_b else {},
        scartati=scartati or [],
    )


@impostazioni_bp.route("/impostazioni/ripristina/<int:scartato_id>", methods=["POST"])
def ripristina(scartato_id):
    """Rimuove un profilo dalla blacklist."""
    db = get_db()
    db.execute("DELETE FROM profili_scartati WHERE id = ?", (scartato_id,))
    db.commit()
    db.close()
    log.info("Profilo %d rimosso dalla blacklist", scartato_id)
    return jsonify({"successo": True})


@impostazioni_bp.route("/impostazioni/salva", methods=["POST"])
def salva():
    """Salva (INSERT o UPDATE) le impostazioni di un profilo."""
    dati = request.get_json()
    profilo = dati.get("profilo")

    if profilo not in ("A", "B"):
        return jsonify({"errore": "Profilo non valido"}), 400

    campi_int = ["eta_min", "eta_max", "anni_esperienza_min",
                 "peso_eta", "peso_esperienza", "peso_settore", "peso_ruolo", "peso_keyword"]
    campi_txt = ["citta", "settori", "istituti", "ruoli_target", "keyword_positive", "keyword_negative"]

    vals = {}
    for c in campi_int:
        try:
            vals[c] = int(dati.get(c, 0))
        except (TypeError, ValueError):
            vals[c] = 0
    for c in campi_txt:
        vals[c] = str(dati.get(c, "") or "").strip()

    db = get_db()
    esistente = db.execute(
        "SELECT id FROM impostazioni_profilo WHERE profilo = ?", (profilo,)
    ).fetchone()

    if esistente:
        db.execute(
            """UPDATE impostazioni_profilo SET
               eta_min=?, eta_max=?, anni_esperienza_min=?,
               citta=?, settori=?, istituti=?, ruoli_target=?,
               keyword_positive=?, keyword_negative=?,
               peso_eta=?, peso_esperienza=?, peso_settore=?,
               peso_ruolo=?, peso_keyword=?,
               data_aggiornamento=CURRENT_TIMESTAMP
               WHERE profilo=?""",
            (vals["eta_min"], vals["eta_max"], vals["anni_esperienza_min"],
             vals["citta"], vals["settori"], vals["istituti"], vals["ruoli_target"],
             vals["keyword_positive"], vals["keyword_negative"],
             vals["peso_eta"], vals["peso_esperienza"], vals["peso_settore"],
             vals["peso_ruolo"], vals["peso_keyword"], profilo)
        )
    else:
        db.execute(
            """INSERT INTO impostazioni_profilo
               (profilo, eta_min, eta_max, anni_esperienza_min,
                citta, settori, istituti, ruoli_target,
                keyword_positive, keyword_negative,
                peso_eta, peso_esperienza, peso_settore,
                peso_ruolo, peso_keyword)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (profilo, vals["eta_min"], vals["eta_max"], vals["anni_esperienza_min"],
             vals["citta"], vals["settori"], vals["istituti"], vals["ruoli_target"],
             vals["keyword_positive"], vals["keyword_negative"],
             vals["peso_eta"], vals["peso_esperienza"], vals["peso_settore"],
             vals["peso_ruolo"], vals["peso_keyword"])
        )

    db.commit()
    db.close()
    return jsonify({"successo": True})
