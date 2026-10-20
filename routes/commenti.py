"""Blueprint «Commenti» — dove Sabia decide sotto quali post farsi vedere.

Una pagina sola, con una riga per post: chi è la persona (e perché ci interessa),
che cosa ha scritto, e la proposta di commento già pronta da correggere.

Le azioni che cambiano qualcosa — generare un testo, approvarlo, segnarlo come
pubblicato — passano tutte da qui e sono esplicite: niente si muove da solo.
Lo scarico dei post costa soldi veri (circa due millesimi di dollaro a post),
quindi non parte mai all'apertura della pagina ma solo su richiesta.
"""

import logging
import threading

from flask import Blueprint, jsonify, render_template, request

from routes.auth import login_required
from services import commenti_linkedin as cl

log = logging.getLogger(__name__)

commenti_bp = Blueprint("commenti", __name__)

# Una passata su centotrenta profili sono quattordici chiamate allo scraper e
# dura minuti: se la si facesse dentro la richiesta, il browser mollerebbe prima
# della fine e il pulsante sembrerebbe rotto mentre il lavoro continua. Quindi
# gira in un thread e la pagina chiede ogni tanto a che punto è.
_ricerca_lock = threading.Lock()
_ricerca = {"in_corso": False, "esito": None}


def _corpo():
    """Il corpo della richiesta, sempre come dizionario.

    `get_json` restituisce quello che gli arriva: una lista, un numero, una
    stringa. Chiamarci `.get()` sopra solleverebbe un'eccezione e il browser
    riceverebbe un errore del server al posto di una spiegazione.
    """
    dati = request.get_json(silent=True)
    return dati if isinstance(dati, dict) else {}


def _testo(dati: dict, chiave: str) -> str:
    """Il valore di un campo, solo se è davvero testo.

    Un numero al posto di una stringa (`{"urn": 123}`) farebbe fallire lo
    `.strip()`: qui diventa semplicemente un campo vuoto, che le route trattano
    già come «manca».
    """
    v = dati.get(chiave)
    return v.strip() if isinstance(v, str) else ""


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
    """Avvia la ricerca dei post nuovi. È l'unico punto in cui si spende."""
    corpo = _corpo()
    try:
        giorni = int(corpo.get("giorni", 7))
    except (TypeError, ValueError):
        giorni = 7
    giorni = min(max(giorni, 1), 30)

    with _ricerca_lock:
        if _ricerca["in_corso"]:
            return jsonify({"ok": False, "errore": "C'è già una ricerca in corso."}), 409
        _ricerca["in_corso"] = True
        _ricerca["esito"] = None

    def _lavora():
        try:
            esito = cl.scarica(giorni=giorni)
        except Exception as e:
            log.error("Scarico post fallito: %s", e, exc_info=True)
            esito = {"ok": False, "errore": str(e)}
        with _ricerca_lock:
            _ricerca["esito"] = esito
            _ricerca["in_corso"] = False

    threading.Thread(target=_lavora, daemon=True).start()
    return jsonify({"ok": True, "avviata": True})


@commenti_bp.route("/commenti/stato-ricerca")
@login_required
def stato_ricerca():
    with _ricerca_lock:
        return jsonify({"in_corso": _ricerca["in_corso"], "esito": _ricerca["esito"]})


@commenti_bp.route("/commenti/genera", methods=["POST"])
@login_required
def genera():
    """Scrive la proposta per un post. Una alla volta: Argon può metterci minuti."""
    urn = _testo(_corpo(), "urn")
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
    corpo = _corpo()
    try:
        commento_id = int(corpo.get("id"))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "errore": "Manca la proposta."}), 400

    testo = corpo.get("testo")
    if testo is not None and not isinstance(testo, str):
        return jsonify({"ok": False, "errore": "Il testo del commento non è testo."}), 400

    esito = cl.decidi(commento_id, _testo(corpo, "azione"), testo)
    if esito.get("ok"):
        return jsonify(esito)
    # Una proposta rifatta altrove non è un errore di chi scrive: è un conflitto,
    # e la pagina lo distingue per avvisare senza far perdere il testo.
    return jsonify(esito), (409 if esito.get("scomparso") else 400)
