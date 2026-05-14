"""
Modulo 4 — Creazione Contenuti LinkedIn.
Genera 3 varianti di post LinkedIn tramite Claude AI.
"""

import urllib.parse
import base64
import requests
from flask import Blueprint, render_template, request, jsonify
from database import get_db
from ai_helpers import genera_contenuti_linkedin, genera_prompt_immagine

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


@contenuti_bp.route("/contenuti/genera_immagine", methods=["POST"])
def genera_immagine():
    """
    Genera un'immagine per il post LinkedIn usando Pollinations.ai (gratuito, no API key).
    Claude costruisce il prompt ottimizzato, Pollinations genera l'immagine con FLUX.
    """
    dati = request.get_json()
    testo_post    = dati.get("testo_post", "").strip()
    tema          = dati.get("tema", "").strip()
    tono          = dati.get("tono", "professionale")
    profilo       = dati.get("profilo", "")
    prompt_custom = dati.get("prompt_custom", "").strip()

    if not testo_post:
        return jsonify({"errore": "Testo post mancante"}), 400

    # Claude genera il prompt ottimizzato per FLUX
    prompt_img = genera_prompt_immagine(testo_post, tema, tono, prompt_custom)

    # Scarica l'immagine dal backend (richiesta anonima, bypassa il login browser)
    prompt_encoded = urllib.parse.quote(prompt_img)
    seed = abs(hash(prompt_img + prompt_custom)) % 99999
    url_pollinations = (
        f"https://image.pollinations.ai/prompt/{prompt_encoded}"
        f"?width=1200&height=628&model=flux&nologo=true&seed={seed}"
    )

    url_pollinations = (
        f"https://image.pollinations.ai/prompt/{prompt_encoded}"
        f"?width=1200&height=628&model=turbo&seed={seed}"
    )
    headers = {"User-Agent": "Mozilla/5.0"}

    # Fino a 3 tentativi con pausa in caso di rate limit (429)
    import time
    for tentativo in range(3):
        try:
            resp_img = requests.get(url_pollinations, timeout=90, headers=headers)
            if resp_img.status_code == 429:
                time.sleep(5)
                continue
            resp_img.raise_for_status()
            # Verifica che sia davvero un'immagine
            if resp_img.content[:4] in (b"<htm", b"<!do", b'{"er'):
                return jsonify({"errore": "Il servizio immagini non è disponibile al momento. Riprova."}), 500
            img_b64 = base64.b64encode(resp_img.content).decode("utf-8")
            content_type = resp_img.headers.get("Content-Type", "image/jpeg")
            data_url = f"data:{content_type};base64,{img_b64}"
            return jsonify({"url": data_url, "prompt": prompt_img})
        except requests.exceptions.RequestException as e:
            if tentativo == 2:
                return jsonify({"errore": f"Errore: {str(e)}"}), 500
            time.sleep(3)

    return jsonify({"errore": "Servizio temporaneamente non disponibile. Riprova tra qualche secondo."}), 500
