"""
Modulo 4 — Creazione Contenuti LinkedIn.
Genera 3 varianti di post LinkedIn tramite Claude AI.
"""

import urllib.parse
import base64
import requests
from flask import Blueprint, render_template, request, jsonify
from database import get_db
from ai_helpers import (
    genera_contenuti_linkedin,
    genera_prompt_immagine,
    modifica_variante_linkedin,
)

# Blueprint per il modulo contenuti
contenuti_bp = Blueprint("contenuti", __name__)


@contenuti_bp.route("/contenuti")
def index():
    """Pagina principale del modulo creazione contenuti."""
    db = get_db()
    # Recupera gli ultimi 10 contenuti generati
    storico = db.execute(
        "SELECT * FROM contenuti_linkedin ORDER BY data_creazione DESC LIMIT 10"
    ).fetchall()
    db.close()
    return render_template("contenuti.html", storico=[dict(s) for s in storico])


@contenuti_bp.route("/contenuti/genera", methods=["POST"])
def genera():
    """Endpoint AJAX per generare i post LinkedIn."""
    dati = request.get_json()
    tema = dati.get("tema", "").strip()
    tono = dati.get("tono", "professionale")
    profilo = dati.get("profilo", "Salvatore Sabia")
    obiettivo = dati.get("obiettivo", "attirare_candidati")
    contesto = dati.get("contesto", "").strip()

    if not tema:
        return jsonify({"errore": "Inserire il tema del post"}), 400

    # Genera le 3 varianti con Claude
    risultato = genera_contenuti_linkedin(tema, tono, profilo, obiettivo, contesto)

    # Salva nel database
    db = get_db()
    db.execute(
        """INSERT INTO contenuti_linkedin
           (tema, tono, profilo_destinazione, obiettivo, contesto,
            variante_1, variante_2, variante_3)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            tema,
            tono,
            profilo,
            obiettivo,
            contesto,
            risultato.get("variante_1", ""),
            risultato.get("variante_2", ""),
            risultato.get("variante_3", ""),
        ),
    )
    db.commit()
    db.close()

    return jsonify(risultato)


@contenuti_bp.route("/contenuti/modifica_variante", methods=["POST"])
def modifica_variante():
    """Chiede a Claude di riscrivere una variante secondo le istruzioni."""
    dati = request.get_json()
    testo_attuale = dati.get("testo_attuale", "").strip()
    richiesta = dati.get("richiesta_modifica", "").strip()

    if not testo_attuale or not richiesta:
        return jsonify({"errore": "Testo e richiesta di modifica obbligatori"}), 400

    testo_nuovo = modifica_variante_linkedin(testo_attuale, richiesta)
    return jsonify({"testo": testo_nuovo})


@contenuti_bp.route("/contenuti/genera_prompt_immagine", methods=["POST"])
def genera_prompt():
    """Claude genera un prompt ottimizzato per la generazione immagine."""
    dati = request.get_json()
    testo_post = dati.get("testo_post", "").strip()

    if not testo_post:
        return jsonify({"errore": "Testo post mancante"}), 400

    prompt = genera_prompt_immagine(testo_post, "", "", "")
    return jsonify({"prompt": prompt})


@contenuti_bp.route("/contenuti/genera_immagine", methods=["POST"])
def genera_immagine():
    """Genera un'immagine da un prompt usando Pollinations.ai."""
    dati = request.get_json()
    prompt_img = dati.get("prompt", "").strip()

    if not prompt_img:
        return jsonify({"errore": "Prompt mancante"}), 400

    prompt_encoded = urllib.parse.quote(prompt_img)
    seed = abs(hash(prompt_img)) % 99999
    url_pollinations = (
        f"https://image.pollinations.ai/prompt/{prompt_encoded}"
        f"?width=1200&height=628&model=turbo&seed={seed}"
    )
    headers = {"User-Agent": "Mozilla/5.0"}

    import time
    for tentativo in range(3):
        try:
            resp_img = requests.get(url_pollinations, timeout=90, headers=headers)
            if resp_img.status_code == 429:
                time.sleep(5)
                continue
            resp_img.raise_for_status()
            if resp_img.content[:4] in (b"<htm", b"<!do", b'{"er'):
                return jsonify({"errore": "Il servizio immagini non e disponibile al momento."}), 500
            img_b64 = base64.b64encode(resp_img.content).decode("utf-8")
            content_type = resp_img.headers.get("Content-Type", "image/jpeg")
            data_url = f"data:{content_type};base64,{img_b64}"
            return jsonify({"url": data_url})
        except requests.exceptions.RequestException as e:
            if tentativo == 2:
                return jsonify({"errore": f"Errore: {str(e)}"}), 500
            time.sleep(3)

    return jsonify({"errore": "Servizio temporaneamente non disponibile."}), 500


@contenuti_bp.route("/contenuti/salva_bozza", methods=["POST"])
def salva_bozza():
    """Salva una bozza di post nel database."""
    dati = request.get_json()
    testo = dati.get("testo", "").strip()
    profilo = dati.get("profilo", "")
    tema = dati.get("tema", "")
    tono = dati.get("tono", "")
    obiettivo = dati.get("obiettivo", "")
    immagine_url = dati.get("immagine_url", "")

    if not testo:
        return jsonify({"errore": "Testo mancante"}), 400

    db = get_db()
    cur = db.execute(
        """INSERT INTO bozze_contenuti
           (profilo, testo, tono, obiettivo, tema, immagine_url)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (profilo, testo, tono, obiettivo, tema, immagine_url),
    )
    db.commit()
    bozza_id = cur.lastrowid
    db.close()

    return jsonify({"successo": True, "id": bozza_id})
