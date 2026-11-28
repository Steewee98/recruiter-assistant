"""
Loop giornaliero: porta ogni giorno nuovi consulenti in «Da valutare».

Cosa fa, una volta al giorno:
  1. cerca su LinkedIn consulenti finanziari CON SEDE A ROMA E NEL LAZIO (poche
     ricerche, a ruoli, pagine e capoluoghi ruotanti, così ogni giorno esce
     gente diversa);
  2. incrocia i profili trovati con l'albo OCF: tiene solo chi risulta iscritto,
     in provincia di Roma o in una delle province intorno (ZONA), sopra i 30
     anni, con un mandato e non del nostro gruppo;
  3. ordina i superstiti per propensione al cambio e scarta chi è già stato
     lavorato;
  4. lancia l'analisi AI sui primi AL_GIORNO;
  5. chi prende almeno il punteggio minimo del bersaglio (7, per Firdaous 6) entra in pipeline come «Da valutare».

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

• LA ZONA DEVE RISULTARE DA ENTRAMBE LE FONTI. L'albo dà il domicilio eletto (di
  norma la residenza), non l'ufficio: da solo non basta a dire che uno lavora a
  Roma o nel Lazio (ZONA). Quindi la sede dichiarata su LinkedIn deve confermarlo, altrimenti il
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
from dataclasses import dataclass
from datetime import date

import requests

from database import get_db

logger = logging.getLogger(__name__)

# ── Criteri richiesti ────────────────────────────────────────────────────────
# Dal 25/11/2026 la zona non è più solo la provincia di Roma ma anche le
# province che le stanno intorno (richiesta di Stefano, per entrambi i giri):
# sono le altre quattro del Lazio. Bacino dell'albo fuori dal gruppo: RM 2412,
# LT 217, FR 158, VT 97, RI 33.
ZONA = ("RM", "LT", "FR", "VT", "RI")
# Capoluoghi in cui cercare su LinkedIn oltre a Roma (una ricerca al giorno).
CAPOLUOGHI_INTORNO = ["Latina", "Frosinone", "Viterbo", "Rieti"]
ETA_MINIMA = 31          # "sopra i 30 anni"
PUNTEGGIO_MINIMO = 7     # entra in «Da valutare» da 7 in su
AL_GIORNO = 10

# Ruoli con cui interrogare LinkedIn, a rotazione: cambiando ruolo ogni giorno
# si pescano persone diverse senza pagare pagine in più.
RUOLI_LINKEDIN = [
    "consulente finanziario", "private banker", "wealth manager",
    "consulente patrimoniale", "financial advisor", "promotore finanziario",
    "financial planner", "family banker", "wealth advisor", "personal banker",
]

# Quante ricerche LinkedIn al massimo per giro. Ogni ricerca costa ~0,10 $ e
# rende in media 2 candidati validi: quattro sono il tetto di spesa giornaliero
# (~0,40 $) oltre il quale non vale la pena insistere.
MAX_RICERCHE = 4
PROFILI_PER_RICERCA = 25

# LinkedIn mostra 10 risultati a pagina: con una pagina sola la ricerca rendeva
# 10 profili anche se il listino «Short» ne paga fino a 25 (log del 3-4/11).
# Tre pagine riempiono i 25 allo stesso prezzo.
PAGINE_PER_RICERCA = 3
# Pagine di partenza a rotazione (1, 4, 7 → pagine 1-9 di ogni ruolo).
PARTENZE = [1 + i * PAGINE_PER_RICERCA for i in range(3)]

# Budget Apify da lasciare intatto per le ricerche fatte a mano. Sotto questa
# soglia l'automazione si ferma da sola.
RISERVA_BUDGET_USD = 2.0

# Lucchetto PostgreSQL: in produzione gunicorn avvia due worker, ognuno con il
# suo pianificatore. Senza, due processi potrebbero lanciare lo stesso giro e
# pagare due volte le stesse ricerche.
LUCCHETTO_LOOP = 918273646


# ── Per chi si cerca ─────────────────────────────────────────────────────────
# Dal 25/11/2026 i giri sono due: quello storico per Salvatore Sabia (consulenti
# di rete, sopra i 30 anni) e uno per Firdaous Filahi (under 35 che lavorano in
# banca). Cambiano ruoli, filtri sull'albo e gestore; ricerca, analisi,
# registro e freni sul budget restano in comune.

# Mandanti dell'albo che sono banche con dipendenti allo sportello, non reti di
# consulenti. L'albo OCF non distingue: la lista è nostra, con i nomi scritti
# esattamente come li normalizza albo_ocf. Fideuram e Intesa Sanpaolo non ci
# sono apposta: sono il nostro gruppo (albo_ocf.GRUPPO_PROPRIO).
# Dubbio aperto (25/11): ING Bank e Mediobanca Premier hanno anche venditori
# fuori sede.
BANCHE_CON_SPORTELLI = frozenset({
    "UniCredit",
    "BNL BNP Paribas", "Poste Italiane", "Credito Emiliano", "Credem Euromobiliare",
    "Credit Agricole Italia", "Banca MPS", "BPER Banca", "Banco Bpm",
    "Deutsche Bank", "Mediobanca Premier", "Ing Bank N V",
    "Banco Di Desio E Della Brianza", "Banco Di Sardegna",
})

# I titoli con cui si presentano su LinkedIn i giovani di filiale.
RUOLI_FIRDAOUS = [
    "gestore clienti", "gestore affluent", "personal banker", "gestore privati",
    "consulente finanziario", "private banker", "relationship manager",
    "consulente investimenti",
]


@dataclass(frozen=True)
class Bersaglio:
    codice: str             # scritto nello storico dei giri
    gestore: str            # "" = deciso dall'età (regola storica del giro di Sabia)
    ruoli: tuple
    eta_minima: int = 0     # 0 = nessun limite
    eta_massima: int = 0    # 0 = nessun limite
    banche: frozenset = frozenset()   # vuoto = qualunque mandante tranne Fideuram
    lucchetto: int = LUCCHETTO_LOOP
    chi: str = ""           # per la nota «nessuno nuovo che …»
    punteggio_minimo: int = PUNTEGGIO_MINIMO   # da quanto si entra in «Da valutare»
    acceso: bool = True     # False = il giro non parte, né di notte né a mano


SABIA = Bersaglio(
    codice="sabia", gestore="", ruoli=tuple(RUOLI_LINKEDIN),
    eta_minima=ETA_MINIMA, chi="nell'albo di Roma e dintorni sopra i 30 anni",
    # Spento il 28/11/2026 su richiesta di Stefano: per Salvatore i nomi
    # arrivano (10 su 10 stanotte), per Firdaous no. Si spende solo per lei.
    # Per riaccenderlo basta togliere questa riga.
    acceso=False,
)
FIRDAOUS = Bersaglio(
    # Dal 28/11/2026 (Stefano: «basta che abbiano l'OCF e siano di Roma e
    # provincia»): niente più limite d'età né obbligo di mandato bancario.
    # Con quei filtri (prima under 35, poi under 45, sempre in banca) il giro ha
    # portato 0 contatti in quattro notti, mentre con i criteri larghi il giro di
    # Salvatore ne portava ~10 a notte. Il giro di Salvatore è spento: i ruoli
    # che rendevano li usa adesso questo, insieme a quelli di filiale.
    # Restano: iscritto all'albo con mandato, zona (ZONA), fuori dal gruppo.
    codice="firdaous", gestore="Firdaous Filahi",
    ruoli=tuple(dict.fromkeys(RUOLI_LINKEDIN + RUOLI_FIRDAOUS)),
    lucchetto=LUCCHETTO_LOOP + 1,
    # 6 invece di 7 (Stefano, 27/11/2026): dopo tre notti Firdaous aveva un solo
    # contatto; il 27/11 l'unico profilo trovato ha preso 5.
    punteggio_minimo=6,
    chi="nell'albo di Roma e dintorni, con un mandato",
)
BERSAGLI = {b.codice: b for b in (SABIA, FIRDAOUS)}


def bersagli_accesi() -> list:
    """I giri che oggi devono partire (vedi Bersaglio.acceso)."""
    return [b for b in BERSAGLI.values() if b.acceso]


_NOMI_ZONA = {"roma", "rome", "lazio", "latium"}
_comuni_rm = set(_NOMI_ZONA)


def _comuni_provincia() -> set:
    """
    Nomi dei comuni della zona (Roma e province intorno), dal nostro stesso albo.
    Servono a riconoscere una sede LinkedIn come «area di Roma» anche quando è
    scritta col nome del comune (Fiumicino, Guidonia, Frascati…).
    """
    global _comuni_rm
    if len(_comuni_rm) > len(_NOMI_ZONA):
        return _comuni_rm
    try:
        db = get_db()
        righe = db.execute(
            f"SELECT DISTINCT comune FROM ocf_iscritti WHERE provincia IN ({','.join(['?'] * len(ZONA))})"
            " AND COALESCE(comune,'') <> ''", ZONA).fetchall()
        db.close()
        _comuni_rm = _comuni_rm | {p for r in righe
                                   for p in _pezzi_sede(r["comune"] or "")}
    except Exception as e:  # pragma: no cover
        logger.warning("Comuni di %s non leggibili: %s", ",".join(ZONA), e)
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
    True se la sede dichiarata su LinkedIn è Roma, il Lazio o un comune delle
    province della ZONA (il nome resta per compatibilità: dal 25/11 la zona
    coincide con il Lazio, quindi anche «Lazio, Italia» da solo basta).

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

    ammessi = _NOMI_ZONA | _comuni_provincia()
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
                         headers={"Authorization": f"Bearer {token}"}, timeout=20)
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


CHIAVE_ATTIVO = "loop_giornaliero_attivo"


def attivo() -> bool:
    """
    L'automazione è accesa?

    Sta in database e non in una variabile d'ambiente perché deve poter cambiare
    con un click dalla pagina, subito, senza un nuovo deploy — e perché il web e
    il pianificatore sono processi diversi che devono leggere lo stesso valore.

    In caso di dubbio (tabella assente, database irraggiungibile) risponde SPENTO:
    un'automazione che si paga a ricerca non deve ripartire da sola per un errore
    di lettura.
    """
    db = get_db()
    try:
        r = db.execute("SELECT valore FROM app_config WHERE chiave = ?",
                       (CHIAVE_ATTIVO,)).fetchone()
    except Exception as e:
        logger.warning("Stato del loop non leggibile, lo considero spento: %s", e)
        return False
    finally:
        db.close()
    if not r:
        return False
    return str(r["valore"]).strip().lower() in ("1", "true", "si", "sì", "on")


def imposta_attivo(nuovo_stato: bool) -> bool:
    """Accende o spegne l'automazione. Ritorna lo stato effettivamente salvato."""
    db = get_db()
    try:
        db.execute("""
            INSERT INTO app_config (chiave, valore, aggiornato_il)
            VALUES (?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT (chiave) DO UPDATE SET valore = EXCLUDED.valore,
                                               aggiornato_il = CURRENT_TIMESTAMP
        """, (CHIAVE_ATTIVO, "1" if nuovo_stato else "0"))
        db.commit()
    finally:
        db.close()
    logger.info("Loop giornaliero %s", "acceso" if nuovo_stato else "spento")
    return bool(nuovo_stato)


def gia_eseguito_oggi(bersaglio: Bersaglio = SABIA) -> bool:
    db = get_db()
    try:
        segnaposto = ",".join(["?"] * len(ESITI_CHE_CONTANO))
        r = db.execute(
            f"""SELECT COUNT(*) AS n FROM ocf_loop_run
                 WHERE DATE(eseguito_il) = CURRENT_DATE
                   AND COALESCE(bersaglio, 'sabia') = ?
                   AND stato IN ({segnaposto})""",
            [bersaglio.codice, *ESITI_CHE_CONTANO],
        ).fetchone()
        return (r or {}).get("n", 0) > 0
    except Exception:
        return False
    finally:
        db.close()


def esegui(limite: int = AL_GIORNO, ignora_budget: bool = False,
           forzato: bool = False, bersaglio: Bersaglio = SABIA) -> dict:
    """
    Un giro completo. Non solleva: ogni errore finisce nel riepilogo e nella
    riga di storico.

    `forzato=True` esegue anche a interruttore spento: è il caso del bottone
    «Esegui un giro adesso», dove la spesa è stata chiesta da una persona in quel
    momento. Il pianificatore automatico non lo usa mai.
    """
    from database import _get_raw_connection
    from services import albo_ocf, dossier_albo

    esito = {"ok": False, "esaminati": 0, "con_linkedin": 0, "analizzati": 0,
             "importati": 0, "scartati_punteggio": 0, "senza_linkedin": 0,
             "fuori_zona": 0, "errori": 0, "dettaglio": [], "nota": "", "budget": None,
             "costo": None, "costo_per_persona": None,
             "ricerche": 0, "profili_linkedin": 0, "bersaglio": bersaglio.codice}

    # 0) Un giro alla volta, anche fra processi diversi
    conn = _get_raw_connection()
    cur = conn.cursor()
    cur.execute("SELECT pg_try_advisory_lock(%s) AS ottenuto", (bersaglio.lucchetto,))
    if not cur.fetchone()["ottenuto"]:
        cur.close()
        conn.close()
        esito["nota"] = "Un altro processo sta già eseguendo il giro."
        return esito

    try:
        return _esegui_protetto(esito, limite, ignora_budget, forzato, bersaglio)
    finally:
        try:
            cur.execute("SELECT pg_advisory_unlock(%s)", (bersaglio.lucchetto,))
        except Exception:
            pass
        cur.close()
        conn.close()


def _esegui_protetto(esito: dict, limite: int, ignora_budget: bool,
                     forzato: bool = False, bersaglio: Bersaglio = SABIA) -> dict:
    from services import albo_ocf, dossier_albo

    # 0-bis) Interruttore: è il primo controllo perché è l'unico gratis. Spento
    #        vuol dire spento anche se budget e AI sarebbero disponibili.
    if not forzato and not attivo():
        esito["nota"] = ("Saltato: il giro automatico è spento. Si riaccende "
                         "dall'interruttore nella pagina Albo.")
        _registra_run(esito, stato="saltata_spento")
        return esito

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
        candidati, ricerche, visti = _raccogli_da_linkedin(limite, bersaglio)
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
                         f"nessuno nuovo che risulti anche {bersaglio.chi}.")
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
            esito["dettaglio"].append({"nome": p["nome_completo"], "esito": "fuori zona",
                                       "motivo": f"su LinkedIn risulta «{sede or 'nessuna sede'}»"})
            continue

        try:
            analisi = _analizza(p, li, bersaglio)
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

        if not isinstance(punteggio, int) or punteggio < bersaglio.punteggio_minimo:
            esito["scartati_punteggio"] += 1
            albo_ocf.registra_dossier(p, linkedin_url=url, esito="scartato_punteggio",
                                      punteggio=punteggio if isinstance(punteggio, int) else None)
            esito["dettaglio"].append({"nome": p["nome_completo"], "esito": "scartato",
                                       "motivo": f"punteggio {punteggio}"})
            continue

        cid, come = _importa(p, li, analisi, bersaglio)
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
    logger.info("Loop giornaliero (%s): %d esaminati, %d con LinkedIn, %d importati",
                bersaglio.codice, esito["esaminati"], esito["con_linkedin"], esito["importati"])
    return esito


def ricerche_del_giorno(giorno: int, ruoli=None) -> list:
    """
    Le coppie (ruolo, pagina di partenza) da usare nel giro del giorno `giorno`
    (date.toordinal()), nell'ordine in cui provarle.

    Ogni giorno prende le MAX_RICERCHE combinazioni successive a quelle del
    giorno prima. La vecchia formula spostava di uno solo il ruolo da un giorno
    all'altro, così tre ricerche su quattro erano identiche a quelle della notte
    prima: si pagavano profili già visti (log Railway del 3-4/11/2026).
    """
    combinazioni = [(r, p) for p in PARTENZE for r in (ruoli or RUOLI_LINKEDIN)]
    inizio = giorno * MAX_RICERCHE
    return [combinazioni[(inizio + t) % len(combinazioni)] for t in range(MAX_RICERCHE)]


def citta_della_ricerca(giorno: int, indice: int) -> str:
    """
    Dove fare la ricerca numero `indice` del giorno: una sola fuori Roma, in un
    capoluogo intorno a rotazione, le altre a Roma. Così la spesa resta
    MAX_RICERCHE al giorno e Roma, che ha quasi tutto il bacino, resta la fonte
    principale.

    La ricerca che va fuori Roma cambia posizione ogni giorno. Se fosse sempre
    l'ultima, con 8 ruoli (Firdaous) e 4 ricerche al giorno toccherebbe sempre
    agli stessi due ruoli, che a Roma non si cercherebbero mai.
    """
    if indice != giorno % MAX_RICERCHE:
        return "Roma"
    return CAPOLUOGHI_INTORNO[giorno % len(CAPOLUOGHI_INTORNO)]


def _raccogli_da_linkedin(quanti: int, bersaglio: Bersaglio = SABIA):
    """
    Cerca su LinkedIn, con i ruoli del bersaglio, persone con sede a Roma e
    tiene solo quelle che risultano anche nell'albo con i criteri del bersaglio
    (vedi _cerca_nellalbo).

    Ritorna (candidati_ordinati_per_propensione, n_ricerche, n_profili_visti).
    Ogni candidato porta con sé il profilo LinkedIn in "_linkedin".
    """
    from routes.ricerca import cerca_apify, normalizza_profilo
    from services import propensione

    # Ruolo e pagina ruotano in base al giorno: senza rotazione ogni giro
    # ripescherebbe gli stessi primi venticinque profili.
    giorno = date.today().toordinal()
    candidati, visti, ricerche = [], 0, 0
    gia_presi, errori = set(), []

    for i, (ruolo, pagina) in enumerate(ricerche_del_giorno(giorno, bersaglio.ruoli)):
        if len(candidati) >= quanti:
            break
        citta = citta_della_ricerca(giorno, i)
        if citta != "Roma":
            pagina = 1      # nei capoluoghi piccoli le pagine oltre la prima sono vuote
        items, errore = cerca_apify(
            ruolo=ruolo, citta=citta, max_items=PROFILI_PER_RICERCA,
            num_pagine=PAGINE_PER_RICERCA, start_page=pagina,
            max_wait=120, modalita="Short",
        )
        ricerche += 1
        if errore:
            logger.warning("Loop: ricerca «%s» pagina %d non riuscita: %s", ruolo, pagina, errore)
            errori.append(errore)
            continue

        visti += len(items or [])
        for item in (items or []):
            if not isinstance(item, dict):
                continue
            li = normalizza_profilo(item)
            riga = _cerca_nellalbo(li.get("nome", ""), li.get("cognome", ""), bersaglio)
            if not riga or riga["chiave"] in gia_presi:
                continue
            gia_presi.add(riga["chiave"])
            riga["_linkedin"] = li
            riga["propensione"] = propensione.coefficiente(riga.get("rete", ""), riga.get("eta"))
            candidati.append(riga)

    # Se TUTTE le ricerche falliscono non è una notte vuota, è un guasto:
    # dal 12/11 la chiave Apify scaduta veniva registrata come «0 profili,
    # nulla da fare» e nessuno se ne accorgeva.
    if ricerche and len(errori) == ricerche:
        raise RuntimeError(f"tutte le {ricerche} ricerche sono fallite. Ultimo errore: {errori[-1]}")

    candidati.sort(key=lambda c: -(c["propensione"].get("indice") or 0))
    return candidati[:quanti], ricerche, visti


def _cerca_nellalbo(nome: str, cognome: str, bersaglio: Bersaglio = SABIA):
    """
    Cerca il nominativo nell'albo con i criteri del bersaglio: provincia
    nella ZONA, età nei limiti, con mandato (per Firdaous: di una banca con
    sportelli), mai del nostro gruppo (Fideuram, Intesa Sanpaolo), non già
    lavorato né in pipeline.
    Ritorna la riga pronta all'uso oppure None.
    """
    nome, cognome = (nome or "").strip(), (cognome or "").strip()
    if not nome or not cognome:
        return None
    anno = date.today().year
    from services.albo_ocf import fuori_dal_gruppo
    fuori, gruppo = fuori_dal_gruppo()
    filtri, valori = [], [*ZONA, *gruppo, nome, cognome]
    if bersaglio.eta_minima:
        filtri.append("AND anno_nascita <= ?")      # almeno eta_minima anni
        valori.append(anno - bersaglio.eta_minima)
    if bersaglio.eta_massima:
        filtri.append("AND anno_nascita >= ?")      # al massimo eta_massima anni
        valori.append(anno - bersaglio.eta_massima)
    if bersaglio.banche:
        banche = sorted(bersaglio.banche)
        filtri.append(f"AND rete IN ({','.join(['?'] * len(banche))})")
        valori.extend(banche)
    # Chi è già stato toccato non torna, TRANNE chi un altro giro ha scartato
    # con un voto che a questo bersaglio basterebbe (Sabia scarta i 6, a
    # Firdaous vanno bene): viene rianalizzato una volta con il profilo giusto.
    # Il dossier si aggiorna col nuovo voto, quindi non si ripete.
    valori.append(bersaglio.punteggio_minimo)
    # Omonimi: dal profilo LinkedIn sappiamo solo nome e cognome. Se nella zona
    # l'albo ha più persone con lo stesso nome, non si sa quale sia: si salta.
    # Prima si prendeva la prima che passava i filtri (LIMIT 1), e allargando la
    # zona (25/11) un profilo di Roma veniva attribuito a un omonimo di Latina
    # del 1960 con un'altra rete: età e mandato sbagliati nell'analisi.
    valori.extend([*ZONA, nome, cognome])
    db = get_db()
    try:
        r = db.execute(f"""
            SELECT chiave, nome, cognome, anno_nascita, comune, provincia, rete, n_cambi
              FROM ocf_iscritti
             WHERE attivo = TRUE AND provincia IN ({','.join(['?'] * len(ZONA))}) AND COALESCE(rete,'') <> '' AND {fuori}
               AND LOWER(nome) = LOWER(?) AND LOWER(cognome) = LOWER(?)
               AND anno_nascita IS NOT NULL {' '.join(filtri)}
               AND chiave NOT IN (SELECT chiave FROM ocf_dossier
                                   WHERE NOT (esito = 'scartato_punteggio'
                                              AND COALESCE(punteggio, 0) >= ?))
               AND (SELECT COUNT(*) FROM ocf_iscritti o
                     WHERE o.attivo = TRUE AND o.provincia IN ({','.join(['?'] * len(ZONA))})
                       AND LOWER(o.nome) = LOWER(?) AND LOWER(o.cognome) = LOWER(?)) = 1
               AND NOT EXISTS (SELECT 1 FROM candidati c
                                WHERE LOWER(c.nome) = LOWER(ocf_iscritti.nome)
                                  AND LOWER(c.cognome) = LOWER(ocf_iscritti.cognome))
             LIMIT 1
        """, valori).fetchone()
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


def _tipo(profilo: dict, bersaglio: Bersaglio) -> str:
    """Profilo di ricerca: B = under 35, A = gli altri (dal 28/11 anche per Firdaous,
    che ora riceve consulenti di ogni età)."""
    eta = profilo.get("eta") or 0
    return "B" if eta and eta < 35 else "A"


def _gestore(tipo: str, bersaglio: Bersaglio) -> str:
    if bersaglio.gestore:
        return bersaglio.gestore
    return "Salvatore Sabia" if tipo == "A" else "Firdaous Filahi"


def _analizza(profilo: dict, linkedin: dict, bersaglio: Bersaglio = SABIA) -> dict:
    """Analisi AI con il profilo di ricerca adatto (B = under 35)."""
    from ai_helpers import analizza_profilo_linkedin
    from routes.albo import _testo_per_analisi

    tipo = _tipo(profilo, bersaglio)
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


def _importa(profilo: dict, linkedin: dict, analisi: dict, bersaglio: Bersaglio = SABIA):
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

    tipo = _tipo(profilo, bersaglio)
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
        if bersaglio.gestore:
            note += f" · giro di {bersaglio.gestore.split()[0]}"
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
             _gestore(tipo, bersaglio)),
        )
        db.commit()
        return cur.lastrowid, "ok"
    except Exception as e:
        logger.error("Loop: inserimento fallito: %s", e, exc_info=True)
        return None, "errore"
    finally:
        db.close()


def _registra_run(esito: dict, stato: str):
    """Scrive il giro nello storico e ritorna l'id della riga (None se non riesce)."""
    import json as _json
    try:
        db = get_db()
        cur = db.execute(
            """INSERT INTO ocf_loop_run (stato, esaminati, con_linkedin, analizzati,
                                         importati, scartati_punteggio, senza_linkedin,
                                         fuori_zona, errori, costo, nota, dettaglio,
                                         bersaglio)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (stato, esito["esaminati"], esito["con_linkedin"], esito["analizzati"],
             esito["importati"], esito["scartati_punteggio"], esito["senza_linkedin"],
             esito.get("fuori_zona", 0), esito["errori"], esito.get("costo"),
             esito.get("nota", ""),
             _json.dumps(esito.get("dettaglio", []), ensure_ascii=False)[:4000],
             esito.get("bersaglio") or "sabia"),
        )
        nuovo_id = getattr(cur, "lastrowid", None)
        db.commit()
        db.close()
        return nuovo_id
    except Exception as e:  # pragma: no cover
        logger.warning("Storico loop non registrato: %s", e)
        return None


def ultime_esecuzioni(quante: int = 10) -> list:
    db = get_db()
    try:
        return [dict(r) for r in db.execute(
            "SELECT * FROM ocf_loop_run ORDER BY id DESC LIMIT ?", (int(quante),)).fetchall()]
    except Exception:
        return []
    finally:
        db.close()
