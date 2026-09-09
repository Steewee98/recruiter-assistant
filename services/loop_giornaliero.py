"""
Loop giornaliero: porta ogni giorno nuovi consulenti in «Da valutare».

Cosa fa, una volta al giorno:
  1. prende dall'albo OCF i consulenti di ROMA (provincia) sopra i 30 anni,
     ordinati per propensione al cambio, saltando chi è già stato lavorato;
  2. ne cerca il profilo LinkedIn — tutti insieme, in un'unica ricerca;
  3. scarta chi non ha un profilo LinkedIn attribuibile con certezza, e chi su
     LinkedIn NON risulta lavorare a Roma;
  4. sui restanti lancia l'analisi AI;
  5. chi prende almeno PUNTEGGIO_MINIMO entra in pipeline come «Da valutare».

Tre vincoli che hanno guidato il progetto:

• IL BUDGET. Le ricerche LinkedIn si pagano a run, circa 0,10 $ l'una, con un
  tetto di 29 $ al mese. Dieci ricerche separate al giorno costerebbero ~29 $ al
  mese: l'intero piano, per questa sola automazione. Per questo i nomi vengono
  cercati A GRUPPI (una run ogni GRANDEZZA_GRUPPO persone), e prima di ogni
  esecuzione si controlla quanto budget resta: se è sotto la riserva, il loop
  salta il giro e lo scrive, invece di consumare i soldi delle ricerche a mano.

• NIENTE LAVORO RIPETUTO. Ogni persona toccata viene registrata, compresi gli
  scarti: senza profilo LinkedIn, o con punteggio basso. Altrimenti domani si
  ripagherebbe la stessa ricerca per riottenere lo stesso "no".

• ROMA DEVE RISULTARE DA ENTRAMBE LE FONTI. L'albo dà il domicilio eletto (di
  norma la residenza), non l'ufficio: da solo non basta a dire che uno lavora a
  Roma. Quindi la sede dichiarata su LinkedIn deve confermarlo, altrimenti il
  candidato viene scartato. Chi non scrive nessuna sede viene scartato anch'esso:
  in un'automazione l'assenza di prova non può valere come prova.

• TRACCIABILITÀ. Ogni esecuzione lascia una riga con quanti ne ha esaminati,
  quanti avevano LinkedIn, quanti importati e perché gli altri no. Un'automazione
  che lavora da sola e non rende conto di cosa ha fatto non è controllabile.
"""

import logging
import os
from datetime import date

import requests

from database import get_db

logger = logging.getLogger(__name__)

# ── Criteri richiesti ────────────────────────────────────────────────────────
PROVINCIA = "RM"
ETA_MINIMA = 31          # "sopra i 30 anni"
PUNTEGGIO_MINIMO = 7     # entra in «Da valutare» da 7 in su
AL_GIORNO = 10

# Quante persone per singola ricerca Apify. Cinque è il compromesso: meno run da
# pagare, ma non tanti nomi da far sparire i meno comuni fra i risultati.
GRANDEZZA_GRUPPO = 5

# Budget Apify da lasciare intatto per le ricerche fatte a mano. Sotto questa
# soglia l'automazione si ferma da sola.
RISERVA_BUDGET_USD = 2.0

# Lucchetto PostgreSQL: in produzione gunicorn avvia due worker, ognuno con il
# suo pianificatore. Senza, due processi potrebbero lanciare lo stesso giro e
# pagare due volte le stesse ricerche.
LUCCHETTO_LOOP = 918273646


_comuni_rm = {"roma", "rome"}


def _comuni_provincia() -> set:
    """
    Nomi dei comuni della provincia operativa, dal nostro stesso albo.
    Servono a riconoscere una sede LinkedIn come «area di Roma» anche quando è
    scritta col nome del comune (Fiumicino, Guidonia, Frascati…).
    """
    global _comuni_rm
    if len(_comuni_rm) > 2:
        return _comuni_rm
    try:
        db = get_db()
        righe = db.execute(
            "SELECT DISTINCT comune FROM ocf_iscritti WHERE provincia = ? AND COALESCE(comune,'') <> ''",
            (PROVINCIA,)).fetchall()
        db.close()
        _comuni_rm = _comuni_rm | {(r["comune"] or "").strip().lower() for r in righe}
    except Exception as e:  # pragma: no cover
        logger.warning("Comuni di %s non leggibili: %s", PROVINCIA, e)
    return _comuni_rm


def lavora_a_roma(location: str) -> bool:
    """
    True se la sede dichiarata su LinkedIn è Roma o un comune della sua provincia.

    Una sede vuota vale NO: l'automazione decide da sola, e non può prendere
    l'assenza di informazione per una conferma.
    """
    testo = (location or "").strip().lower()
    if not testo:
        return False
    if "roma" in testo or "rome" in testo:
        return True
    return any(c in testo for c in _comuni_provincia() if len(c) > 4)


def budget_apify() -> dict:
    """
    Quanto budget Apify resta nel ciclo corrente.
    Ritorna {"noto": bool, "usato": float, "tetto": float, "residuo": float}.
    In caso di dubbio dichiara "non noto": il chiamante decide se rischiare.
    """
    token = os.environ.get("APIFY_API_KEY", "")
    if not token:
        return {"noto": False, "motivo": "APIFY_API_KEY non configurata"}
    try:
        r = requests.get("https://api.apify.com/v2/users/me/limits",
                         params={"token": token}, timeout=20)
        r.raise_for_status()
        d = r.json()["data"]
        usato = float((d.get("current") or {}).get("monthlyUsageUsd", 0))
        tetto = float((d.get("limits") or {}).get("maxMonthlyUsageUsd", 0))
        return {"noto": True, "usato": round(usato, 2), "tetto": round(tetto, 2),
                "residuo": round(tetto - usato, 2),
                "fine_ciclo": (d.get("monthlyUsageCycle") or {}).get("endAt", "")[:10]}
    except Exception as e:
        logger.warning("Budget Apify non leggibile: %s", e)
        return {"noto": False, "motivo": str(e)}


def gia_eseguito_oggi() -> bool:
    db = get_db()
    try:
        r = db.execute(
            "SELECT COUNT(*) AS n FROM ocf_loop_run WHERE DATE(eseguito_il) = CURRENT_DATE"
        ).fetchone()
        return (r or {}).get("n", 0) > 0
    except Exception:
        return False
    finally:
        db.close()


def esegui(limite: int = AL_GIORNO, ignora_budget: bool = False) -> dict:
    """
    Un giro completo. Non solleva: ogni errore finisce nel riepilogo e nella
    riga di storico.
    """
    from database import _get_raw_connection
    from services import albo_ocf, dossier_albo

    esito = {"ok": False, "esaminati": 0, "con_linkedin": 0, "analizzati": 0,
             "importati": 0, "scartati_punteggio": 0, "senza_linkedin": 0,
             "fuori_zona": 0, "errori": 0, "dettaglio": [], "nota": "", "budget": None}

    # 0) Un giro alla volta, anche fra processi diversi
    conn = _get_raw_connection()
    cur = conn.cursor()
    cur.execute("SELECT pg_try_advisory_lock(%s) AS ottenuto", (LUCCHETTO_LOOP,))
    if not cur.fetchone()["ottenuto"]:
        cur.close()
        conn.close()
        esito["nota"] = "Un altro processo sta già eseguendo il giro."
        return esito

    try:
        return _esegui_protetto(esito, limite, ignora_budget)
    finally:
        try:
            cur.execute("SELECT pg_advisory_unlock(%s)", (LUCCHETTO_LOOP,))
        except Exception:
            pass
        cur.close()
        conn.close()


def _esegui_protetto(esito: dict, limite: int, ignora_budget: bool) -> dict:
    from services import albo_ocf, dossier_albo

    # 1) Freno sul budget: prima di spendere, guarda quanto resta
    b = budget_apify()
    esito["budget"] = b
    if not ignora_budget and b.get("noto") and b["residuo"] < RISERVA_BUDGET_USD:
        esito["nota"] = (f"Saltato: budget Apify quasi esaurito "
                         f"({b['usato']}$ di {b['tetto']}$, restano {b['residuo']}$). "
                         f"Riparte da solo al rinnovo del {b.get('fine_ciclo') or 'ciclo'}.")
        _registra_run(esito, stato="saltata_budget")
        return esito

    # 2) Chi lavorare oggi
    try:
        trovati = albo_ocf.cerca(provincia=PROVINCIA, eta_min=ETA_MINIMA,
                                 limite=limite, escludi_lavorati=True)["profili"]
    except Exception as e:
        logger.error("Loop: ricerca albo fallita: %s", e, exc_info=True)
        esito["nota"] = f"Ricerca nell'albo non riuscita: {e}"
        _registra_run(esito, stato="errore")
        return esito

    if not trovati:
        esito["ok"] = True
        esito["nota"] = ("Nessun consulente nuovo con questi criteri "
                         f"(provincia {PROVINCIA}, oltre {ETA_MINIMA - 1} anni).")
        _registra_run(esito, stato="nulla_da_fare")
        return esito

    esito["esaminati"] = len(trovati)

    # Costo reale del giro: si legge il consumo Apify prima e dopo. La
    # convenienza della ricerca a gruppi è una previsione finché non la si
    # misura — così il primo giro vero la conferma o la smentisce da solo.
    usato_prima = b.get("usato") if b.get("noto") else None

    # 3) LinkedIn a gruppi: una sola ricerca ogni GRANDEZZA_GRUPPO persone
    profili_li = {}
    for i in range(0, len(trovati), GRANDEZZA_GRUPPO):
        gruppo = trovati[i:i + GRANDEZZA_GRUPPO]
        try:
            profili_li.update(dossier_albo.cerca_linkedin_gruppo(gruppo))
        except Exception as e:
            logger.error("Loop: ricerca LinkedIn fallita: %s", e, exc_info=True)
            esito["errori"] += 1

    # 4) Analisi e importazione
    for p in trovati:
        ris = profili_li.get(p["chiave"]) or {}
        li = ris.get("profilo")
        nota_li = ris.get("nota") or ""

        # Senza un profilo attribuibile con certezza non si va avanti: l'analisi
        # su un omonimo sbagliato produrrebbe un candidato inventato.
        incerto = bool(nota_li) and ("verificare" in nota_li or "arbitraria" in nota_li)
        if not li or incerto:
            esito["senza_linkedin"] += 1
            albo_ocf.registra_dossier(p, esito="senza_linkedin")
            esito["dettaglio"].append({"nome": p["nome_completo"], "esito": "senza LinkedIn",
                                       "motivo": nota_li or "nessun profilo attribuibile"})
            continue

        esito["con_linkedin"] += 1

        # Roma deve risultare anche da LinkedIn: l'albo dice dove abita, non
        # dove lavora. Senza questa conferma il candidato non è del territorio.
        sede = li.get("location", "")
        if not lavora_a_roma(sede):
            esito["fuori_zona"] += 1
            albo_ocf.registra_dossier(p, linkedin_url=li.get("linkedin", ""),
                                      esito="fuori_zona")
            esito["dettaglio"].append({"nome": p["nome_completo"], "esito": "fuori Roma",
                                       "motivo": f"su LinkedIn risulta «{sede or 'nessuna sede'}»"})
            continue

        try:
            analisi = _analizza(p, li)
        except Exception as e:
            from ai_helpers import messaggio_errore_ai
            logger.error("Loop: analisi fallita per %s: %s", p["nome_completo"], e)
            esito["errori"] += 1
            esito["dettaglio"].append({"nome": p["nome_completo"], "esito": "errore analisi",
                                       "motivo": messaggio_errore_ai(e)})
            continue

        esito["analizzati"] += 1
        punteggio = analisi.get("punteggio")
        url = li.get("linkedin", "")

        if not isinstance(punteggio, int) or punteggio < PUNTEGGIO_MINIMO:
            esito["scartati_punteggio"] += 1
            albo_ocf.registra_dossier(p, linkedin_url=url, esito="scartato_punteggio",
                                      punteggio=punteggio if isinstance(punteggio, int) else None)
            esito["dettaglio"].append({"nome": p["nome_completo"], "esito": "scartato",
                                       "motivo": f"punteggio {punteggio}"})
            continue

        cid = _importa(p, li, analisi)
        if cid:
            esito["importati"] += 1
            albo_ocf.registra_dossier(p, linkedin_url=url, esito="in_pipeline",
                                      punteggio=punteggio, candidato_id=cid)
            esito["dettaglio"].append({"nome": p["nome_completo"], "esito": "importato",
                                       "motivo": f"punteggio {punteggio}"})
        else:
            albo_ocf.registra_dossier(p, linkedin_url=url, esito="gia_presente",
                                      punteggio=punteggio)
            esito["dettaglio"].append({"nome": p["nome_completo"], "esito": "già in pipeline",
                                       "motivo": ""})

    if usato_prima is not None:
        dopo = budget_apify()
        if dopo.get("noto"):
            esito["costo"] = round(max(0.0, dopo["usato"] - usato_prima), 4)
            esito["budget"] = dopo
            if esito["esaminati"]:
                esito["costo_per_persona"] = round(esito["costo"] / esito["esaminati"], 4)

    esito["ok"] = True
    _registra_run(esito, stato="completata")
    logger.info("Loop giornaliero: %d esaminati, %d con LinkedIn, %d importati",
                esito["esaminati"], esito["con_linkedin"], esito["importati"])
    return esito


def _analizza(profilo: dict, linkedin: dict) -> dict:
    """Analisi AI con il profilo di ricerca adatto all'età (B = under 35)."""
    from ai_helpers import analizza_profilo_linkedin
    from routes.albo import _testo_per_analisi

    eta = profilo.get("eta") or 0
    tipo = "B" if eta and eta < 35 else "A"
    db = get_db()
    try:
        imp = db.execute("SELECT * FROM impostazioni_profilo WHERE profilo = ?",
                         (tipo,)).fetchone()
    finally:
        db.close()
    testo = _testo_per_analisi(profilo, {
        "ruolo": linkedin.get("ruolo", ""), "sommario": linkedin.get("sommario", ""),
        "url": linkedin.get("linkedin", ""),
    })
    return analizza_profilo_linkedin(testo, tipo, imp)


def _importa(profilo: dict, linkedin: dict, analisi: dict):
    """
    Inserisce il candidato in «Da valutare». Ritorna l'id, o None se risulta
    già in pipeline. Lo stato è «Da valutare» e non «Da contattare» proprio
    perché la scelta l'ha fatta una macchina: prima ci passa una persona.
    """
    import json as _json

    from dedup import is_duplicate

    eta = profilo.get("eta") or 0
    tipo = "B" if eta and eta < 35 else "A"
    url = linkedin.get("linkedin", "")
    db = get_db()
    try:
        dup, _motivo, _cid = is_duplicate(db, {
            "nome": profilo.get("nome", ""), "cognome": profilo.get("cognome", ""),
            "azienda": profilo.get("rete", ""), "linkedin": url,
        })
        if dup:
            return None

        prop = profilo.get("propensione") or {}
        citta = ", ".join(x for x in [profilo.get("comune", ""), profilo.get("provincia", "")] if x)
        note = f"Trovato dal loop giornaliero · albo OCF · {citta}"
        if prop.get("indice"):
            note += f" · propensione {prop['indice']}x la media"
        spunti = analisi.get("spunti_contatto") or []
        cur = db.execute(
            """INSERT INTO candidati
               (nome, cognome, ruolo_attuale, azienda, note, profilo_linkedin,
                tipo_profilo, stato, punteggio, analisi, spunti, messaggio_outreach,
                source, url_fonte, gestore)
               VALUES (?, ?, ?, ?, ?, ?, ?, 'Da valutare', ?, ?, ?, ?, 'ocf', ?, ?)""",
            (profilo.get("nome", ""), profilo.get("cognome", ""),
             linkedin.get("ruolo") or "Consulente finanziario",
             profilo.get("rete", ""), note, url, tipo,
             analisi.get("punteggio"), analisi.get("analisi_percorso") or "",
             _json.dumps(spunti if isinstance(spunti, list) else [], ensure_ascii=False),
             analisi.get("messaggio_outreach") or "", url,
             "Salvatore Sabia" if tipo == "A" else "Firdaous Filahi"),
        )
        db.commit()
        return cur.lastrowid
    except Exception as e:
        logger.error("Loop: inserimento fallito: %s", e, exc_info=True)
        return None
    finally:
        db.close()


def _registra_run(esito: dict, stato: str) -> None:
    import json as _json
    try:
        db = get_db()
        db.execute(
            """INSERT INTO ocf_loop_run (stato, esaminati, con_linkedin, analizzati,
                                         importati, scartati_punteggio, senza_linkedin,
                                         fuori_zona, errori, costo, nota, dettaglio)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (stato, esito["esaminati"], esito["con_linkedin"], esito["analizzati"],
             esito["importati"], esito["scartati_punteggio"], esito["senza_linkedin"],
             esito.get("fuori_zona", 0), esito["errori"], esito.get("costo"),
             esito.get("nota", ""),
             _json.dumps(esito.get("dettaglio", []), ensure_ascii=False)[:4000]),
        )
        db.commit()
        db.close()
    except Exception as e:  # pragma: no cover
        logger.warning("Storico loop non registrato: %s", e)


def ultime_esecuzioni(quante: int = 10) -> list:
    db = get_db()
    try:
        return [dict(r) for r in db.execute(
            "SELECT * FROM ocf_loop_run ORDER BY id DESC LIMIT ?", (int(quante),)).fetchall()]
    except Exception:
        return []
    finally:
        db.close()
