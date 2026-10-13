"""
«Chi chiamare adesso»: la lista dei consulenti dell'albo da lavorare per primi,
ordinata per propensione misurata al cambio di rete.

COSA FA E COSA NON FA. Ordina, non prevede. Il coefficiente viene da
`services/propensione.py`, che è stato validato fuori campione — parametri
stimati sui soli elenchi 2022, verificati sui passaggi 2023-2026 mai visti:
ordinando per coefficiente, nei primi 500 nomi il 32,6% ha poi cambiato rete
contro l'8,6% della popolazione (lift 3,8x). Tradotto per chi telefona:
partendo dalla cima si parla con persone che cambiano rete circa quattro volte
più spesso della media. Non vuol dire che quella persona cambierà.

PERCHÉ NON USA «GLI SPOSTAMENTI RECENTI» COME SEGNALE INDIVIDUALE. Sembrava la
variabile ovvia — se da una rete escono in cinque, il sesto è pronto a muoversi —
ed è stata misurata: avere colleghi usciti di recente dalla stessa rete e
provincia dà lift 0,92x, cioè nessun segnale (anzi, un soffio sotto la media).
Anche essersi già mossi in passato non dice nulla (1,00x). Quello che i
passaggi osservati servono davvero a fare è stimare il TASSO DI USCITA PER
RETE, che da solo vale 3,5x. La rete di appartenenza e l'età sono tutto ciò che
i dati sostengono.

PERCHÉ LA LISTA È CONGELATA E NON RICALCOLATA A OGNI APERTURA. Chi telefona
lavora la lista per giorni: se l'ordine cambia sotto le mani, non si capisce più
a che punto si era arrivati. La classifica viene quindi rigenerata una volta a
settimana, dopo la sincronizzazione dell'albo, e resta ferma fino alla
successiva. Il guadagno settimanale non sta nel ricalcolo dei tassi — quelli si
muovono lentissimo — ma nel fatto che le persone cambiano rete: chi la settimana
scorsa era in una rete stabile e oggi è in una mobile si sposta di parecchio.
"""

import json
import logging
from datetime import date

from database import get_db

logger = logging.getLogger(__name__)

# Quanti nomi tenere. Oltre il migliaio la lista non viene più lavorata da
# nessuno, e tenerla corta è anche l'unico modo perché la cima resti densa:
# il lift si misura sui primi k, e cresce al diminuire di k.
QUANTI = 300

# Età minima: sotto i 31 l'esperienza è troppo breve perché il passaggio abbia
# senso per l'ufficio. È lo stesso criterio del giro giornaliero.
ETA_MINIMA = 31


def genera(quanti: int = QUANTI) -> dict:
    """
    Ricalcola la classifica e la salva. Ritorna un riepilogo.

    Non solleva: un errore qui non deve far fallire la sincronizzazione
    settimanale che la richiama.
    """
    from services import albo_ocf, propensione

    esito = {"ok": False, "quanti": 0, "bacino": 0, "nota": ""}
    try:
        par = propensione.parametri(ricalcola=True)
        if not par.get("pronto"):
            esito["nota"] = par.get("motivo", "Parametri di propensione non stimabili.")
            return esito

        db = get_db()
        try:
            # Il bacino: iscritti attivi della provincia operativa, con un
            # mandato in corso, non già nostri, non già lavorati (dossier) e non
            # già in pipeline. Chi è stato toccato non deve ricomparire: la
            # lista serve a dire «chi NON hai ancora chiamato».
            righe = db.execute("""
                SELECT chiave, nome, cognome, anno_nascita, comune, provincia, rete
                  FROM ocf_iscritti
                 WHERE attivo = TRUE AND provincia = ?
                   AND COALESCE(rete,'') <> '' AND rete <> ?
                   AND anno_nascita IS NOT NULL AND anno_nascita <= ?
                   AND chiave NOT IN (SELECT chiave FROM ocf_dossier)
                   AND NOT EXISTS (SELECT 1 FROM candidati c
                                    WHERE LOWER(c.nome) = LOWER(ocf_iscritti.nome)
                                      AND LOWER(c.cognome) = LOWER(ocf_iscritti.cognome))
            """, (albo_ocf.PROVINCIA_OPERATIVA, albo_ocf.RETE_PROPRIA,
                  date.today().year - ETA_MINIMA)).fetchall()
        finally:
            db.close()

        esito["bacino"] = len(righe)
        if not righe:
            esito["nota"] = "Nessun nominativo lavorabile rimasto nel bacino."
            _salva([], esito["bacino"])
            esito["ok"] = True
            return esito

        anno = date.today().year
        classifica = []
        for r in righe:
            d = dict(r)
            d["eta"] = anno - d["anno_nascita"] if d.get("anno_nascita") else None
            c = propensione.coefficiente(d.get("rete", ""), d["eta"], par)
            if not c.get("disponibile"):
                continue
            d["propensione"] = c
            classifica.append(d)

        classifica.sort(key=lambda x: (-(x["propensione"].get("indice") or 0),
                                       x["cognome"] or ""))
        classifica = classifica[:quanti]
        _salva(classifica, esito["bacino"])

        esito["ok"] = True
        esito["quanti"] = len(classifica)
        logger.info("Classifica «chi chiamare»: %d nomi su un bacino di %d",
                    esito["quanti"], esito["bacino"])
        return esito
    except Exception as e:  # pragma: no cover
        logger.error("Classifica «chi chiamare» non generata: %s", e, exc_info=True)
        esito["nota"] = str(e)
        return esito


def _salva(classifica: list, bacino: int) -> None:
    """
    Sostituisce la classifica precedente con quella nuova, in UNA transazione.

    Svuotare e riscrivere senza transazione lascerebbe la pagina vuota per
    qualche secondo ogni domenica notte, proprio mentre qualcuno potrebbe
    guardarla.
    """
    db = get_db()
    try:
        db.execute("DELETE FROM ocf_da_chiamare")
        for posizione, p in enumerate(classifica, start=1):
            prop = p.get("propensione") or {}
            db.execute("""
                INSERT INTO ocf_da_chiamare
                    (chiave, posizione, nome, cognome, rete, comune, provincia, eta,
                     indice, probabilita_annua, perche, bacino)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (p["chiave"], posizione, p.get("nome", ""), p.get("cognome", ""),
                  p.get("rete", ""), p.get("comune", ""), p.get("provincia", ""),
                  p.get("eta"), prop.get("indice"), prop.get("probabilita_annua"),
                  json.dumps(prop.get("perche") or [], ensure_ascii=False), bacino))
        db.commit()
    finally:
        db.close()


def leggi(limite: int = 50) -> dict:
    """La classifica salvata, pronta per la pagina."""
    db = get_db()
    try:
        righe = [dict(r) for r in db.execute(
            "SELECT * FROM ocf_da_chiamare ORDER BY posizione LIMIT ?",
            (int(limite),)).fetchall()]
    except Exception as e:
        logger.warning("Classifica non leggibile: %s", e)
        return {"righe": [], "generata_il": "", "bacino": 0, "totale": 0}
    finally:
        db.close()

    for r in righe:
        try:
            r["perche"] = json.loads(r.get("perche") or "[]")
        except Exception:
            r["perche"] = []

    db = get_db()
    try:
        tot = db.execute("SELECT COUNT(*) AS n FROM ocf_da_chiamare").fetchone()
    finally:
        db.close()

    return {"righe": righe,
            "generata_il": (righe[0].get("generata_il") or "") if righe else "",
            "bacino": (righe[0].get("bacino") or 0) if righe else 0,
            "totale": (tot or {}).get("n", 0)}


def serve_rigenerare(giorni: int = 7) -> bool:
    """True se la classifica manca o ha più di `giorni` giorni."""
    db = get_db()
    try:
        r = db.execute("""SELECT MAX(generata_il) AS ultima FROM ocf_da_chiamare""").fetchone()
    except Exception:
        return True
    finally:
        db.close()
    ultima = (r or {}).get("ultima")
    if not ultima:
        return True
    try:
        giorno = date.fromisoformat(str(ultima)[:10])
    except Exception:
        return True
    return (date.today() - giorno).days >= giorni
