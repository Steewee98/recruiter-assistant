"""
Dalle interazioni LinkedIn al bacino dell'albo.

IL PROBLEMA. Sui contenuti di Sabia misuriamo le impressioni, che è la metrica
della piattaforma: quanti occhi ha deciso di darci LinkedIn. La domanda che conta
per un ufficio che recluta è un'altra: di quelli che hanno commentato o guardato
il profilo, quanti sono consulenti finanziari di Roma che potremmo chiamare?

Quella risposta ce l'abbiamo solo noi, perché abbiamo l'albo: 2.185 nominativi
lavorabili in provincia. Un post con 1.200 impressioni e zero persone del bacino
ha fallito; uno con 200 impressioni e sette consulenti dentro ha funzionato.

NIENTE AI QUI DENTRO. Riconoscere «Marco Rossi» dentro un elenco di nomi è
confronto fra stringhe, non comprensione del linguaggio: usare un modello
aggiungerebbe costo, latenza e un margine di errore su un compito che il codice
risolve in modo esatto e ripetibile.

L'OMONIMIA NON SI INDOVINA. Se due persone nell'albo si chiamano allo stesso
modo, l'esito è «ambiguo» e la riga non viene attribuita a nessuno. Attribuirla
al primo della lista gonfierebbe la metrica proprio nel punto in cui serve
pulita, e manderebbe Sabia a parlare con la persona sbagliata.
"""

import logging
import re
import unicodedata
from datetime import date

from database import get_db

logger = logging.getLogger(__name__)

# Esiti possibili del riconoscimento di un nome.
RICONOSCIUTO = "riconosciuto"      # una sola corrispondenza nel bacino
AMBIGUO = "ambiguo"                # più persone con lo stesso nome: non si assegna
FUORI = "fuori_bacino"             # non è nell'albo di questa provincia

_indice = {"chiave_cache": None, "mappa": None}


def _normalizza(testo: str) -> str:
    """
    Riduce un nome alla sua forma confrontabile: niente accenti, niente
    punteggiatura, spazi singoli, tutto minuscolo.

    «Nicolò D'Angelo » e «NICOLO D ANGELO» devono cadere sulla stessa stringa,
    altrimenti metà dei nomi italiani non verrebbe mai riconosciuta.
    """
    piatto = unicodedata.normalize("NFKD", testo or "")
    piatto = "".join(c for c in piatto if not unicodedata.combining(c)).lower()
    piatto = re.sub(r"[^a-z0-9\s]", " ", piatto)
    return re.sub(r"\s+", " ", piatto).strip()


def _carica_indice(forza: bool = False) -> dict:
    """
    Indice in memoria del bacino: nome completo normalizzato → lista di chiavi.

    Il bacino è di poche migliaia di righe: tenerlo in memoria evita una query
    per ogni nome da riconoscere, che su un elenco di cento commenti sarebbero
    cento interrogazioni al database per un confronto fra stringhe.
    """
    from services import albo_ocf

    oggi = date.today().isoformat()
    if not forza and _indice["chiave_cache"] == oggi and _indice["mappa"] is not None:
        return _indice["mappa"]

    db = get_db()
    try:
        righe = db.execute("""
            SELECT chiave, nome, cognome, rete, comune, provincia, anno_nascita
              FROM ocf_iscritti
             WHERE attivo = TRUE AND provincia = ? AND COALESCE(rete,'') <> ''
        """, (albo_ocf.PROVINCIA_OPERATIVA,)).fetchall()
    finally:
        db.close()

    mappa = {}
    for r in righe:
        d = dict(r)
        nome, cognome = d.get("nome") or "", d.get("cognome") or ""
        # Due chiavi per ogni persona: «nome cognome» e «cognome nome». Su
        # LinkedIn l'ordine non è garantito, e alcune reti esportano al contrario.
        for forma in (f"{nome} {cognome}", f"{cognome} {nome}"):
            k = _normalizza(forma)
            if k:
                mappa.setdefault(k, []).append(d)

    _indice["chiave_cache"] = oggi
    _indice["mappa"] = mappa
    logger.info("Indice albo per LinkedIn: %d nomi su %d iscritti", len(mappa), len(righe))
    return mappa


def riconosci(nomi: list) -> list:
    """
    Per ogni nome dice se è una persona del bacino, chi, o perché non lo sappiamo.
    Ritorna una lista di dizionari, uno per nome, nell'ordine ricevuto.
    """
    mappa = _carica_indice()
    fuori = []
    for grezzo in nomi:
        pulito = _normalizza(grezzo)
        if not pulito or len(pulito.split()) < 2:
            # Un nome di una parola sola non è attribuibile: su LinkedIn capita
            # con i profili che mostrano solo il nome o un soprannome.
            fuori.append({"nome_mostrato": grezzo, "esito": FUORI,
                          "motivo": "nome incompleto", "persona": None})
            continue

        trovati = mappa.get(pulito, [])
        if len(trovati) == 1:
            p = dict(trovati[0])
            p["eta"] = (date.today().year - p["anno_nascita"]) if p.get("anno_nascita") else None
            fuori.append({"nome_mostrato": grezzo, "esito": RICONOSCIUTO,
                          "motivo": "", "persona": p})
        elif len(trovati) > 1:
            fuori.append({"nome_mostrato": grezzo, "esito": AMBIGUO,
                          "motivo": f"{len(trovati)} omonimi nell'albo", "persona": None})
        else:
            fuori.append({"nome_mostrato": grezzo, "esito": FUORI,
                          "motivo": "non nell'albo di Roma", "persona": None})
    return fuori


def registra(nomi: list, tipo: str = "commento", post: str = "") -> dict:
    """
    Riconosce un elenco di nomi e lo archivia. `tipo` distingue commento,
    reazione o visita al profilo: il peso dei tre non è lo stesso e tenerli
    insieme renderebbe la metrica inutile.
    """
    esiti = riconosci(nomi)
    db = get_db()
    try:
        for e in esiti:
            db.execute("""
                INSERT INTO linkedin_interazioni (nome_mostrato, chiave, esito, tipo, post)
                VALUES (?, ?, ?, ?, ?)
            """, (e["nome_mostrato"][:200],
                  (e["persona"] or {}).get("chiave"),
                  e["esito"], tipo, (post or "")[:200]))
        db.commit()
    finally:
        db.close()

    dentro = [e for e in esiti if e["esito"] == RICONOSCIUTO]
    return {
        "totale": len(esiti),
        "nel_bacino": len(dentro),
        "ambigui": sum(1 for e in esiti if e["esito"] == AMBIGUO),
        "fuori": sum(1 for e in esiti if e["esito"] == FUORI),
        "quota_utile": round(len(dentro) / len(esiti) * 100, 1) if esiti else 0.0,
        "persone": [e["persona"] for e in dentro],
        "esiti": esiti,
    }


def riepilogo(giorni: int = 30) -> dict:
    """
    La metrica del periodo: quante persone DISTINTE del bacino sono state
    raggiunte. Distinte, non interazioni: lo stesso consulente che commenta
    cinque post è una persona sola, e contarlo cinque volte racconterebbe una
    copertura che non esiste.
    """
    db = get_db()
    try:
        tot = db.execute("""
            SELECT COUNT(*) AS n FROM linkedin_interazioni
             WHERE rilevato_il > CURRENT_DATE - ?
        """, (int(giorni),)).fetchone()
        distinte = db.execute("""
            SELECT COUNT(DISTINCT chiave) AS n FROM linkedin_interazioni
             WHERE rilevato_il > CURRENT_DATE - ? AND chiave IS NOT NULL
        """, (int(giorni),)).fetchone()
        per_tipo = db.execute("""
            SELECT tipo, COUNT(DISTINCT chiave) AS persone, COUNT(*) AS interazioni
              FROM linkedin_interazioni
             WHERE rilevato_il > CURRENT_DATE - ?
             GROUP BY tipo ORDER BY persone DESC
        """, (int(giorni),)).fetchall()
        per_post = db.execute("""
            SELECT post,
                   COUNT(*) AS interazioni,
                   COUNT(DISTINCT CASE WHEN chiave IS NOT NULL THEN chiave END) AS nel_bacino
              FROM linkedin_interazioni
             WHERE rilevato_il > CURRENT_DATE - ? AND COALESCE(post,'') <> ''
             GROUP BY post ORDER BY nel_bacino DESC LIMIT 15
        """, (int(giorni),)).fetchall()
    except Exception as e:
        logger.warning("Riepilogo LinkedIn non leggibile: %s", e)
        return {"giorni": giorni, "interazioni": 0, "persone_bacino": 0,
                "per_tipo": [], "per_post": []}
    finally:
        db.close()

    return {
        "giorni": giorni,
        "interazioni": (tot or {}).get("n", 0),
        "persone_bacino": (distinte or {}).get("n", 0),
        "per_tipo": [dict(r) for r in per_tipo],
        "per_post": [dict(r) for r in per_post],
    }


def profili_da_commentare(limite: int = 15) -> list:
    """
    Chi commentare questa settimana, e con quale cognizione di causa.

    NON scrive il commento. Un commento generato in automatico si riconosce, e
    LinkedIn ha detto apertamente che quel genere di attività non conta nulla:
    il valore qui è far arrivare Sabia davanti al profilo sapendo già con chi ha
    a che fare, così scrive due righe vere in trenta secondi.

    Ordina per propensione misurata al cambio di rete ed esclude chi è già stato
    lavorato o è già in pipeline.
    """
    from services import albo_ocf, propensione

    par = propensione.parametri()
    anno = date.today().year
    db = get_db()
    try:
        righe = db.execute("""
            SELECT i.chiave, i.nome, i.cognome, i.rete, i.comune, i.anno_nascita,
                   d.linkedin_url,
                   (SELECT COUNT(*) FROM linkedin_interazioni li
                     WHERE li.chiave = i.chiave) AS gia_interagito
              FROM ocf_iscritti i
         LEFT JOIN ocf_dossier d ON d.chiave = i.chiave
             WHERE i.attivo = TRUE AND i.provincia = ?
               AND COALESCE(i.rete,'') <> '' AND i.rete <> ?
               AND i.anno_nascita IS NOT NULL
               AND i.chiave NOT IN (SELECT chiave FROM ocf_dossier WHERE esito = 'in_pipeline')
               AND NOT EXISTS (SELECT 1 FROM candidati c
                                WHERE LOWER(c.nome) = LOWER(i.nome)
                                  AND LOWER(c.cognome) = LOWER(i.cognome))
        """, (albo_ocf.PROVINCIA_OPERATIVA, albo_ocf.RETE_PROPRIA)).fetchall()
    finally:
        db.close()

    fuori = []
    for r in righe:
        d = dict(r)
        d["eta"] = anno - d["anno_nascita"] if d.get("anno_nascita") else None
        c = propensione.coefficiente(d.get("rete", ""), d["eta"], par)
        if not c.get("disponibile"):
            continue
        d["propensione"] = c
        d["contesto"] = _contesto(d, c)
        fuori.append(d)

    fuori.sort(key=lambda x: (-(x["propensione"].get("indice") or 0), x["cognome"] or ""))
    return fuori[:limite]


def _contesto(persona: dict, coef: dict) -> dict:
    """
    Cosa sappiamo di questa persona e cosa ne consegue per il tono del commento.

    I numeri citati vengono dai nostri stessi dati (tassi di uscita per rete
    misurati sull'albo) e dalle ricerche di settore sulla portabilità: servono a
    Sabia per dire qualcosa di non generico, non per essere scritti nel commento.
    """
    eta = persona.get("eta") or 0
    indice = coef.get("indice") or 1.0

    if indice >= 2.0:
        temperatura = "alta"
        angolo = ("Viene da una rete con uscite sopra la media: è il profilo che "
                  "si guarda intorno senza che nessuno glielo chieda.")
    elif indice >= 1.3:
        temperatura = "media"
        angolo = ("Rete con mobilità nella norma: conta più il momento personale "
                  "che la rete di provenienza.")
    else:
        temperatura = "bassa"
        angolo = ("Viene da una rete stabile: qui un contatto diretto suona "
                  "fuori luogo, meglio presenza costante e nessuna fretta.")

    if eta and eta < 35:
        fascia = "under 35, la fascia che si muove di più (circa 1,3 volte la media)"
    elif eta and eta >= 60:
        fascia = "sopra i 60, fascia che si muove circa la metà della media"
    else:
        fascia = f"{eta} anni, mobilità in linea con la media" if eta else "età non nota"

    return {
        "temperatura": temperatura,
        "angolo": angolo,
        "fascia": fascia,
        "perche": coef.get("perche", []),
        # Regole di ingaggio: valgono più di qualunque suggerimento di testo.
        "da_evitare": [
            "proporre un colloquio o un cambio rete dentro il commento",
            "citare dati sul suo portafoglio o sulla sua rete: sono cose che non "
            "dovremmo sapere e che mettono a disagio",
            "complimenti generici («bel post», «condivido in pieno»): non lasciano traccia",
        ],
        "da_fare": [
            "rispondere nel merito con un'esperienza concreta o un numero",
            "farsi riconoscere come qualcuno del mestiere, non come un reclutatore",
            "lasciare che sia lui a guardare il profilo di Sabia",
        ],
    }
