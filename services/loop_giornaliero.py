"""
Loop giornaliero: porta ogni giorno nuovi consulenti in «Da valutare».

Cosa fa, una volta al giorno:
  1. cerca su LinkedIn consulenti finanziari CON SEDE A ROMA (poche ricerche, a
     ruoli e pagine ruotanti, così ogni giorno esce gente diversa);
  2. incrocia i profili trovati con l'albo OCF: tiene solo chi risulta iscritto,
     in provincia di Roma, sopra i 30 anni, con un mandato e non già in Fideuram;
  3. ordina i superstiti per propensione al cambio e scarta chi è già stato
     lavorato;
  4. lancia l'analisi AI sui primi AL_GIORNO;
  5. chi prende almeno PUNTEGGIO_MINIMO entra in pipeline come «Da valutare».

⚠️ PERCHÉ IL FLUSSO PARTE DA LINKEDIN E NON DALL'ALBO. La prima versione faceva
il contrario: prendeva i più propensi dall'albo e ne cercava il profilo LinkedIn
uno per uno. Provata sul campo, è risultata sbagliata due volte:
  • l'actor si paga 0,10 $ A RICERCA, non a persona: una ricerca per nome serve
    UNA persona, una per ruolo+città ne serve venticinque;
  • cercare più nomi insieme non funziona — l'actor incrocia nomi e cognomi e
    restituisce una sola combinazione (chiedendo cinque persone tornavano dieci
    omonimi di «Alessia Colasanti», che non era nessuna delle cinque).
Il giro reale del 10/09 ha fatto 10 esaminati, 0 trovati, 0,27 $ spesi. Partendo
da LinkedIn, la stessa spesa rende ~2 candidati validi per ricerca.

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
import re
import unicodedata
from datetime import date

import requests

from database import get_db

logger = logging.getLogger(__name__)

# ── Criteri richiesti ────────────────────────────────────────────────────────
PROVINCIA = "RM"
ETA_MINIMA = 31          # "sopra i 30 anni"
PUNTEGGIO_MINIMO = 7     # entra in «Da valutare» da 7 in su
AL_GIORNO = 10

# Ruoli con cui interrogare LinkedIn, a rotazione: cambiando ruolo ogni giorno
# si pescano persone diverse senza pagare pagine in più.
RUOLI_LINKEDIN = [
    "consulente finanziario", "private banker", "wealth manager",
    "consulente patrimoniale", "financial advisor", "promotore finanziario",
]

# Quante ricerche LinkedIn al massimo per giro. Ogni ricerca costa ~0,10 $ e
# rende in media 2 candidati validi: quattro sono il tetto di spesa giornaliero
# (~0,40 $) oltre il quale non vale la pena insistere.
MAX_RICERCHE = 4
PROFILI_PER_RICERCA = 25

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
        _comuni_rm = _comuni_rm | {p for r in righe
                                   for p in _pezzi_sede(r["comune"] or "")}
    except Exception as e:  # pragma: no cover
        logger.warning("Comuni di %s non leggibili: %s", PROVINCIA, e)
    return _comuni_rm


# Parole di contorno nelle sedi LinkedIn: vanno tolte prima del confronto,
# altrimenti «Rome Metropolitan Area» non corrisponde a «Roma».
_RUMORE_SEDE = ("metropolitan area", "metropolitan", "greater", "area",
                "provincia di", "province of", "citta metropolitana di",
                "citta metropolitana", "region", "regione")


def _pezzi_sede(location: str) -> list:
    """
    Spezza una sede LinkedIn nei suoi componenti geografici, normalizzati.
    «Rome, Latium, Italy» → ['rome', 'latium', 'italy'].
    """
    piatto = unicodedata.normalize("NFKD", location or "")
    piatto = "".join(c for c in piatto if not unicodedata.combining(c)).lower()
    pezzi = []
    for grezzo in re.split(r"[,/|]", piatto):
        pezzo = grezzo.strip()
        for rumore in _RUMORE_SEDE:
            pezzo = pezzo.replace(rumore, " ")
        pezzo = re.sub(r"[^a-z0-9' ]+", " ", pezzo)
        pezzo = re.sub(r"\s+", " ", pezzo).strip()
        if pezzo:
            pezzi.append(pezzo)
    return pezzi


def lavora_a_roma(location: str) -> bool:
    """
    True se la sede dichiarata su LinkedIn è Roma o un comune della sua provincia.

    Il confronto è per COMPONENTE INTERA, non per sottostringa. Cercare "roma"
    dentro il testo sembrava innocuo e invece faceva passare mezza Italia:
    «Bologna, Emilia-Romagna» contiene "roma", e così «Bucharest, Romania» e
    «Forlì, Emilia-Romagna». Un'automazione che importa candidati da Bologna
    perché la regione si chiama Romagna è peggio di un'automazione che non
    importa niente.

    Una sede vuota vale NO: l'automazione decide da sola, e non può prendere
    l'assenza di informazione per una conferma.
    """
    pezzi = _pezzi_sede(location)
    if not pezzi:
        return False

    # Se il paese è dichiarato e non è l'Italia, è fuori a prescindere.
    paesi_ok = {"italy", "italia", "it"}
    ultimo = pezzi[-1]
    if len(pezzi) > 1 and ultimo not in paesi_ok and ultimo in _PAESI_NOTI:
        return False

    ammessi = {"roma", "rome"} | _comuni_provincia()
    return any(p in ammessi for p in pezzi)


# Paesi che compaiono spesso come ultimo componente di una sede LinkedIn: se
# c'è un paese e non è l'Italia, la sede è fuori zona qualunque cosa dica il resto.
_PAESI_NOTI = {
    "italy", "italia", "romania", "france", "francia", "spain", "spagna",
    "germany", "germania", "switzerland", "svizzera", "united kingdom", "uk",
    "united states", "usa", "belgium", "belgio", "austria", "portugal",
    "portogallo", "netherlands", "olanda", "poland", "polonia", "greece",
    "grecia", "san marino", "monaco", "luxembourg", "lussemburgo", "malta",
    "croatia", "croazia", "slovenia", "albania", "brazil", "brasile",
}


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


# Esiti che consumano la giornata: il lavoro è stato fatto (o non c'era niente
# da fare). Un giro SALTATO — credito AI finito, budget esaurito, un altro
# processo che teneva il lucchetto — non conta: altrimenti un intoppo passeggero
# delle 6 del mattino brucerebbe l'intera giornata, e ci si accorgerebbe il
# giorno dopo che non è entrato nessuno.
ESITI_CHE_CONTANO = ("completata", "nulla_da_fare")


def gia_eseguito_oggi() -> bool:
    db = get_db()
    try:
        segnaposto = ",".join(["?"] * len(ESITI_CHE_CONTANO))
        r = db.execute(
            f"""SELECT COUNT(*) AS n FROM ocf_loop_run
                 WHERE DATE(eseguito_il) = CURRENT_DATE
                   AND stato IN ({segnaposto})""",
            list(ESITI_CHE_CONTANO),
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
             "fuori_zona": 0, "errori": 0, "dettaglio": [], "nota": "", "budget": None,
             "costo": None, "costo_per_persona": None,
             "ricerche": 0, "profili_linkedin": 0}

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

    # 1-bis) L'AI risponde? Si verifica PRIMA di spendere in ricerche.
    #        Il giro del 10/09 ha bruciato 0,30 $ di ricerche per poi scoprire
    #        che il credito Anthropic era finito: dieci candidati trovati e
    #        nessuno analizzabile. Una chiamata da dieci token lo evita.
    from ai_helpers import test_connessione_api
    prova_ai = test_connessione_api()
    if not prova_ai.get("ok"):
        from ai_helpers import messaggio_errore_ai
        esito["nota"] = ("Saltato: l'AI non risponde, e senza analisi le ricerche "
                         "sarebbero soldi buttati — " + messaggio_errore_ai(Exception(prova_ai.get("errore", ""))))
        _registra_run(esito, stato="saltata_ai")
        return esito

    # 2) LinkedIn per primo: è il passo che si paga, e una sola ricerca serve
    #    venticinque persone invece di una.
    usato_prima = b.get("usato") if b.get("noto") else None
    try:
        candidati, ricerche, visti = _raccogli_da_linkedin(limite)
    except Exception as e:
        logger.error("Loop: raccolta da LinkedIn fallita: %s", e, exc_info=True)
        esito["nota"] = f"Ricerca LinkedIn non riuscita: {e}"
        _registra_run(esito, stato="errore")
        return esito

    esito["ricerche"] = ricerche
    esito["profili_linkedin"] = visti
    esito["esaminati"] = len(candidati)

    if not candidati:
        esito["ok"] = True
        esito["nota"] = (f"{visti} profili LinkedIn esaminati in {ricerche} ricerche, "
                         "nessuno nuovo che risulti anche nell'albo di Roma sopra i 30 anni.")
        _registra_run(esito, stato="nulla_da_fare")
        return esito

    # 3) Analisi e importazione
    for p in candidati:
        li = p.pop("_linkedin")
        esito["con_linkedin"] += 1

        sede = li.get("location", "")
        if not lavora_a_roma(sede):
            esito["fuori_zona"] += 1
            albo_ocf.registra_dossier(p, linkedin_url=li.get("linkedin", ""), esito="fuori_zona")
            esito["dettaglio"].append({"nome": p["nome_completo"], "esito": "fuori Roma",
                                       "motivo": f"su LinkedIn risulta «{sede or 'nessuna sede'}»"})
            continue

        try:
            analisi = _analizza(p, li)
        except Exception as e:
            from ai_helpers import messaggio_errore_ai
            logger.error("Loop: analisi fallita per %s: %s", p["nome_completo"], e)
            esito["errori"] += 1
            messaggio = messaggio_errore_ai(e)
            esito["dettaglio"].append({"nome": p["nome_completo"], "esito": "errore analisi",
                                       "motivo": messaggio})
            # Credito finito o chiave non valida: gli altri falliranno uguale.
            # Ci si ferma qui e li si lascia liberi per il giro di domani.
            if any(x in messaggio.lower() for x in ("credito", "autenticazione", "chiave")):
                esito["nota"] = f"Interrotto dopo {esito['analizzati']} analisi — {messaggio}"
                break
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

        cid, come = _importa(p, li, analisi)
        if come == "ok":
            esito["importati"] += 1
            albo_ocf.registra_dossier(p, linkedin_url=url, esito="in_pipeline",
                                      punteggio=punteggio, candidato_id=cid)
            esito["dettaglio"].append({"nome": p["nome_completo"], "esito": "importato",
                                       "motivo": f"punteggio {punteggio}"})
        elif come == "duplicato":
            albo_ocf.registra_dossier(p, linkedin_url=url, esito="gia_presente", punteggio=punteggio)
            esito["dettaglio"].append({"nome": p["nome_completo"], "esito": "già in pipeline",
                                       "motivo": ""})
        else:
            # Errore tecnico: NON si registra nulla, così domani si riprova.
            esito["errori"] += 1
            esito["dettaglio"].append({"nome": p["nome_completo"], "esito": "errore salvataggio",
                                       "motivo": "riprovato al prossimo giro"})

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


def _raccogli_da_linkedin(quanti: int):
    """
    Cerca su LinkedIn consulenti con sede a Roma e tiene solo quelli che
    risultano anche nell'albo, in provincia di Roma, sopra i 30 anni, con un
    mandato e non già in Fideuram.

    Ritorna (candidati_ordinati_per_propensione, n_ricerche, n_profili_visti).
    Ogni candidato porta con sé il profilo LinkedIn in "_linkedin".
    """
    from routes.ricerca import cerca_apify, normalizza_profilo
    from services import propensione

    # Ruolo e pagina ruotano in base al giorno: senza rotazione ogni giro
    # ripescherebbe gli stessi primi venticinque profili.
    giorno = date.today().toordinal()
    candidati, visti, ricerche = [], 0, 0
    gia_presi = set()

    for tentativo in range(MAX_RICERCHE):
        if len(candidati) >= quanti:
            break
        ruolo = RUOLI_LINKEDIN[(giorno + tentativo) % len(RUOLI_LINKEDIN)]
        pagina = ((giorno + tentativo) // len(RUOLI_LINKEDIN)) % 5 + 1
        items, errore = cerca_apify(
            ruolo=ruolo, citta="Roma", max_items=PROFILI_PER_RICERCA,
            start_page=pagina, max_wait=120, modalita="Short",
        )
        ricerche += 1
        if errore:
            logger.warning("Loop: ricerca «%s» pagina %d non riuscita: %s", ruolo, pagina, errore)
            continue

        visti += len(items or [])
        for item in (items or []):
            if not isinstance(item, dict):
                continue
            li = normalizza_profilo(item)
            riga = _cerca_nellalbo(li.get("nome", ""), li.get("cognome", ""))
            if not riga or riga["chiave"] in gia_presi:
                continue
            gia_presi.add(riga["chiave"])
            riga["_linkedin"] = li
            riga["propensione"] = propensione.coefficiente(riga.get("rete", ""), riga.get("eta"))
            candidati.append(riga)

    candidati.sort(key=lambda c: -(c["propensione"].get("indice") or 0))
    return candidati[:quanti], ricerche, visti


def _cerca_nellalbo(nome: str, cognome: str):
    """
    Cerca il nominativo nell'albo con i criteri del giro: provincia operativa,
    sopra l'età minima, con mandato, non Fideuram, non già lavorato né in
    pipeline. Ritorna la riga pronta all'uso oppure None.
    """
    nome, cognome = (nome or "").strip(), (cognome or "").strip()
    if not nome or not cognome:
        return None
    anno_limite = date.today().year - ETA_MINIMA
    db = get_db()
    try:
        r = db.execute("""
            SELECT chiave, nome, cognome, anno_nascita, comune, provincia, rete, n_cambi
              FROM ocf_iscritti
             WHERE attivo = TRUE AND provincia = ? AND COALESCE(rete,'') <> '' AND rete <> ?
               AND LOWER(nome) = LOWER(?) AND LOWER(cognome) = LOWER(?)
               AND anno_nascita IS NOT NULL AND anno_nascita <= ?
               AND chiave NOT IN (SELECT chiave FROM ocf_dossier)
               AND NOT EXISTS (SELECT 1 FROM candidati c
                                WHERE LOWER(c.nome) = LOWER(ocf_iscritti.nome)
                                  AND LOWER(c.cognome) = LOWER(ocf_iscritti.cognome))
             LIMIT 1
        """, (PROVINCIA, albo_rete_propria(), nome, cognome, anno_limite)).fetchone()
    finally:
        db.close()
    if not r:
        return None
    d = dict(r)
    d["eta"] = date.today().year - d["anno_nascita"] if d.get("anno_nascita") else None
    d["nome_completo"] = f"{d.get('nome','')} {d.get('cognome','')}".strip()
    return d


def albo_rete_propria() -> str:
    from services.albo_ocf import RETE_PROPRIA
    return RETE_PROPRIA


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
    Inserisce il candidato in «Da valutare».
    Ritorna (id, esito) con esito in {"ok", "duplicato", "errore"}.

    I tre casi vanno distinti, non ridotti a "id oppure None": un INSERT fallito
    per un problema tecnico verrebbe scambiato per un duplicato, la persona
    finirebbe segnata come già lavorata e sparirebbe per sempre dai giri
    successivi senza essere mai stata importata.

    Lo stato è «Da valutare» e non «Da contattare» perché la scelta l'ha fatta
    una macchina: prima ci passa una persona.
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
            return None, "duplicato"

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
        return cur.lastrowid, "ok"
    except Exception as e:
        logger.error("Loop: inserimento fallito: %s", e, exc_info=True)
        return None, "errore"
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
