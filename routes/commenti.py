"""Blueprint «Commenti» — dove Sabia decide sotto quali post farsi vedere.

Una pagina sola, con una riga per post: chi è la persona (e perché ci interessa),
che cosa ha scritto, e la proposta di commento già pronta da correggere.

Le azioni che cambiano qualcosa — generare un testo, approvarlo, segnarlo come
pubblicato — passano tutte da qui e sono esplicite: niente si muove da solo.
Lo scarico dei post costa soldi veri (circa due millesimi di dollaro a post),
quindi non parte mai all'apertura della pagina ma solo su richiesta.
"""

import logging

from flask import Blueprint, jsonify, render_template, request

from routes.auth import login_required
from services import commenti_linkedin as cl

log = logging.getLogger(__name__)

commenti_bp = Blueprint("commenti", __name__)


@commenti_bp.route("/commenti")
@login_required
def index():
    return render_template("commenti.html",
                           post=cl.da_approvare(),
                           riepilogo=cl.riepilogo())


@commenti_bp.route("/commenti/dati")
@login_required
def dati():
    """Serve a ridisegnare la pagina dopo un'azione, senza ricaricarla."""
    return jsonify({"ok": True, "post": cl.da_approvare(), "riepilogo": cl.riepilogo()})


@commenti_bp.route("/commenti/scarica", methods=["POST"])
@login_required
def scarica():
    """Cerca i post nuovi. È l'unico punto in cui si spende."""
    corpo = request.get_json(silent=True) or {}
    try:
        giorni = int(corpo.get("giorni", 7))
    except (TypeError, ValueError):
        giorni = 7
    try:
        esito = cl.scarica(giorni=giorni)
    except Exception as e:
        log.error("Scarico post fallito: %s", e, exc_info=True)
        return jsonify({"ok": False, "errore": str(e)}), 500
    return jsonify(esito)


@commenti_bp.route("/commenti/genera", methods=["POST"])
@login_required
def genera():
    """Scrive la proposta per un post. Una alla volta: Argon può metterci minuti."""
    corpo = request.get_json(silent=True) or {}
    urn = (corpo.get("urn") or "").strip()
    if not urn:
        return jsonify({"ok": False, "errore": "Manca il post."}), 400
    try:
        return jsonify(cl.genera_proposta(urn))
    except Exception as e:
        log.error("Generazione commento fallita per %s: %s", urn, e, exc_info=True)
        return jsonify({"ok": False, "errore": str(e)}), 500


@commenti_bp.route("/commenti/decidi", methods=["POST"])
@login_required
def decidi():
    """Approva (con le eventuali correzioni di Sabia), scarta, o segna pubblicato."""
    corpo = request.get_json(silent=True) or {}
    try:
        commento_id = int(corpo.get("id"))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "errore": "Manca la proposta."}), 400
    esito = cl.decidi(commento_id, (corpo.get("azione") or "").strip(), corpo.get("testo"))
    return jsonify(esito), (200 if esito.get("ok") else 400)
