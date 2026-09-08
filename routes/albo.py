"""
Blueprint Albo OCF — ricerca sulla popolazione ufficiale dei consulenti finanziari
e radar dei passaggi di rete.

Differenza rispetto a /ricerca (LinkedIn via Apify):
  • /ricerca      → campiona LinkedIn, costa, restituisce ~20 profili per volta,
                    e non sa se la persona è davvero un consulente iscritto.
  • /albo         → interroga l'elenco ufficiale OCF già scaricato in database:
                    istantaneo, gratuito, completo per rete e per città, e ogni
                    riga è per definizione un CF abilitato.

L'importazione in pipeline da qui NON passa dall'AI: sono dati anagrafici
verificati dall'Organismo, non serve un giudizio del modello per salvarli
(l'analisi resta disponibile dopo, sul singolo candidato).
"""

import logging
import os
import threading
import time

from flask import Blueprint, jsonify, render_template, request

from database import get_db
from dedup import is_duplicate
from routes.auth import login_required
from services import albo_ocf

log = logging.getLogger(__name__)

albo_bp = Blueprint("albo", __name__)

# Stato della sincronizzazione in corso. Gunicorn gira con 1 worker e 4 thread
# (vedi gunicorn.conf.py), quindi un dizionario di modulo è sufficiente e non
# richiede un job persistente.
_sync_stato = {"in_corso": False, "esito": None}
_sync_lock = threading.Lock()

# Ogni quanti giorni riscaricare l'elenco ufficiale. Sette è un compromesso:
# abbastanza fitto da non perdere passaggi di rete, abbastanza rado da non
# martellare il portale OCF.
GIORNI_CADENZA = 7

# Ogni quanto il pianificatore ricontrolla se è ora di aggiornare. Non è la
# cadenza dell'aggiornamento (quella è GIORNI_CADENZA): è solo la frequenza con
# cui si guarda l'orologio, così un riavvio di Railway non fa saltare la settimana.
CONTROLLO_OGNI_SECONDI = 6 * 3600


def _avvia_sync(elenco: str = "abilitati") -> bool:
    """Avvia la sincronizzazione in un thread. False se ce n'è già una in corso."""
    with _sync_lock:
        if _sync_stato["in_corso"]:
            return False
        _sync_stato["in_corso"] = True
        _sync_stato["esito"] = None

    def _lavora():
        try:
            esito = albo_ocf.sincronizza(elenco)
        except Exception as e:  # pragma: no cover — la sync cattura già da sé
            log.error("Sync albo fallita: %s", e, exc_info=True)
            esito = {"ok": False, "errore": str(e)}
        with _sync_lock:
            _sync_stato["esito"] = esito
            _sync_stato["in_corso"] = False

    threading.Thread(target=_lavora, daemon=True).start()
    return True


def _forse_sincronizza_auto() -> bool:
    """
    Se l'ultimo aggiornamento riuscito ha più di GIORNI_CADENZA giorni, ne avvia
    uno in background.

    Perché automatico: i passaggi di rete esistono solo come DIFFERENZA fra due
    elenchi. Saltare le sincronizzazioni non è un ritardo, è perdita definitiva
    di dati — i movimenti avvenuti nel mezzo non sono più ricostruibili, e sono
    le etichette su cui si potrà addestrare qualunque modello di propensione.
    """
    if not albo_ocf.serve_aggiornamento(GIORNI_CADENZA):
        return False
    log.info("Albo OCF: ultimo aggiornamento oltre %d giorni, sincronizzo", GIORNI_CADENZA)
    return _avvia_sync()


def _pianificatore():
    """
    Tiene l'elenco aggiornato una volta a settimana senza dipendere dal fatto che
    qualcuno apra la pagina.

    Perché serve un thread e non basta il controllo all'apertura: i passaggi di
    rete esistono solo come differenza fra due elenchi, e una settimana saltata
    è persa per sempre. Se nessuno entra nell'app per dieci giorni, quei dieci
    giorni di movimenti non si recuperano più.
    """
    while True:
        try:
            if albo_ocf.serve_aggiornamento(GIORNI_CADENZA):
                log.info("Pianificatore albo: avvio aggiornamento settimanale")
                _avvia_sync()
        except Exception as e:  # pragma: no cover — il thread non deve mai morire
            log.warning("Pianificatore albo: %s", e)
        time.sleep(CONTROLLO_OGNI_SECONDI)


def avvia_pianificatore() -> bool:
    """
    Attiva il pianificatore. Spento di default in locale: non vogliamo che un
    test o una sessione di sviluppo scarichino 56.000 righe. In produzione si
    accende da solo (Railway espone RAILWAY_ENVIRONMENT), oppure si forza con
    ALBO_SYNC_AUTO=1.
    """
    attivo = os.environ.get("ALBO_SYNC_AUTO") == "1" or bool(os.environ.get("RAILWAY_ENVIRONMENT"))
    if not attivo:
        return False
    threading.Thread(target=_pianificatore, daemon=True, name="albo-sync").start()
    log.info("Pianificatore albo attivo: controllo ogni %d ore, cadenza %d giorni",
             CONTROLLO_OGNI_SECONDI // 3600, GIORNI_CADENZA)
    return True


@albo_bp.route("/albo")
@login_required
def index():
    _forse_sincronizza_auto()
    stats = albo_ocf.statistiche()
    # 1826 giorni ≈ 5 anni: lo storico ricostruito parte dal 2022
    ultimi_movimenti = albo_ocf.movimenti(tipo="cambio_rete", giorni=1826, limite=50)
    # Solo il caso che serve a questo ufficio: gruppi ENTRATI in Fideuram sulla
    # piazza di Roma. Il valore operativo non è il gruppo (già nostro) ma i
    # colleghi rimasti nella banca di partenza.
    squadre = albo_ocf.squadre_in_movimento(
        min_persone=2, limite=25, verso=albo_ocf.RETE_PROPRIA,
        solo_provincia=albo_ocf.PROVINCIA_OPERATIVA)
    nuove = albo_ocf.squadre_in_movimento(
        min_persone=2, limite=25, verso=albo_ocf.RETE_PROPRIA,
        solo_provincia=albo_ocf.PROVINCIA_OPERATIVA, solo_non_viste=True)
    return render_template("albo.html", stats=stats, movimenti=ultimi_movimenti,
                           squadre=squadre, squadre_nuove=nuove,
                           provincia=albo_ocf.PROVINCIA_OPERATIVA,
                           sync_in_corso=_sync_stato["in_corso"])


@albo_bp.route("/albo/squadre-viste", methods=["POST"])
@login_required
def squadre_viste():
    """Archivia la segnalazione: le squadre già lette non ricompaiono in evidenza."""
    n = albo_ocf.segna_squadre_viste(provincia=albo_ocf.PROVINCIA_OPERATIVA,
                                     verso=albo_ocf.RETE_PROPRIA)
    return jsonify({"ok": True, "archiviati": n})


@albo_bp.route("/albo/storico", methods=["POST"])
@login_required
def storico():
    """
    Ricostruisce i passaggi passati dagli elenchi archiviati (Wayback Machine).
    Operazione lunga (scarica più archivi): gira in background come la sync.
    """
    with _sync_lock:
        if _sync_stato["in_corso"]:
            return jsonify({"ok": False, "errore": "Un aggiornamento è già in corso."}), 409
        _sync_stato["in_corso"] = True
        _sync_stato["esito"] = None

    def _lavora():
        try:
            esito = albo_ocf.importa_storico()
        except Exception as e:
            log.error("Import storico fallito: %s", e, exc_info=True)
            esito = {"ok": False, "errore": str(e)}
        with _sync_lock:
            _sync_stato["esito"] = esito
            _sync_stato["in_corso"] = False

    threading.Thread(target=_lavora, daemon=True).start()
    return jsonify({"ok": True, "avviata": True})


@albo_bp.route("/albo/sincronizza", methods=["POST"])
@login_required
def sincronizza():
    """
    Avvia lo scaricamento dell'elenco ufficiale in background e risponde subito:
    56.000 righe richiedono più del tempo di una richiesta HTTP.
    """
    elenco = (request.get_json() or {}).get("elenco", "abilitati")
    if not _avvia_sync(elenco):
        return jsonify({"ok": False, "errore": "Una sincronizzazione è già in corso."}), 409
    return jsonify({"ok": True, "avviata": True})


@albo_bp.route("/albo/stato-sync")
@login_required
def stato_sync():
    with _sync_lock:
        return jsonify({"in_corso": _sync_stato["in_corso"], "esito": _sync_stato["esito"]})


@albo_bp.route("/albo/cerca", methods=["POST"])
@login_required
def cerca():
    """
    Ricerca nell'albo. Nessuna chiamata esterna: è una query locale.
    Segnala quali risultati sono già in pipeline invece di nasconderli:
    su una fonte completa "già visto" è un'informazione, non uno scarto.
    """
    d = request.get_json() or {}
    try:
        ris = albo_ocf.cerca(
            rete=d.get("rete", ""), comune=d.get("comune", ""),
            provincia=d.get("provincia", ""), regione=d.get("regione", ""),
            eta_min=d.get("eta_min") or None, eta_max=d.get("eta_max") or None,
            limite=min(int(d.get("limite") or 100), 500),
            offset=int(d.get("offset") or 0),
            escludi_lavorati=d.get("escludi_lavorati", True),
        )
    except Exception as e:
        log.error("Ricerca albo fallita: %s", e, exc_info=True)
        return jsonify({"errore": f"Ricerca nell'albo non riuscita: {e}"}), 500

    db = get_db()
    try:
        for p in ris["profili"]:
            dup, motivo, cid = is_duplicate(db, {
                "nome": p["nome"], "cognome": p["cognome"], "azienda": p.get("rete", ""),
            })
            p["gia_in_pipeline"] = bool(dup)
            p["motivo_dup"] = motivo if dup else ""
            p["candidato_id"] = cid
    finally:
        db.close()

    ris["nuovi"] = sum(1 for p in ris["profili"] if not p["gia_in_pipeline"])
    return jsonify(ris)


@albo_bp.route("/albo/importa", methods=["POST"])
@login_required
def importa():
    """
    Porta in pipeline i profili scelti dall'albo. Senza AI e senza Apify:
    nome, cognome, rete e città bastano per iniziare a lavorare il contatto.
    """
    profili = (request.get_json() or {}).get("profili") or []
    if not profili:
        return jsonify({"errore": "Nessun profilo selezionato."}), 400

    db = get_db()
    inseriti, saltati = 0, 0
    try:
        for p in profili:
            nome = (p.get("nome") or "").strip()
            cognome = (p.get("cognome") or "").strip()
            rete = (p.get("rete") or "").strip()
            if not cognome:
                saltati += 1
                continue
            dup, _motivo, _cid = is_duplicate(db, {"nome": nome, "cognome": cognome, "azienda": rete})
            if dup:
                saltati += 1
                continue
            citta = ", ".join(x for x in [p.get("comune", ""), p.get("provincia", "")] if x)
            note = f"Da albo OCF · {citta}"
            if p.get("eta"):
                note += f" · {p['eta']} anni"
            if p.get("n_cambi"):
                note += f" · {p['n_cambi']} cambio/i di rete rilevati"
            db.execute(
                """INSERT INTO candidati (nome, cognome, ruolo_attuale, azienda, note,
                                          tipo_profilo, stato, source, url_fonte)
                   VALUES (?, ?, ?, ?, ?, ?, ?, 'ocf', '')""",
                (nome, cognome, "Consulente finanziario", rete, note,
                 p.get("tipo_profilo") or "A", "Da valutare"),
            )
            inseriti += 1
            albo_ocf.registra_dossier(p, esito="in_pipeline")
        db.commit()
    except Exception as e:
        log.error("Import da albo fallito: %s", e, exc_info=True)
        return jsonify({"errore": f"Import non riuscito: {e}"}), 500
    finally:
        db.close()

    return jsonify({"ok": True, "inseriti": inseriti, "saltati": saltati})


@albo_bp.route("/albo/colleghi", methods=["POST"])
@login_required
def colleghi():
    """Chi è ancora nella rete che ha appena perso una squadra, stessa provincia."""
    d = request.get_json() or {}
    rete, provincia = (d.get("rete") or "").strip(), (d.get("provincia") or "").strip()
    if not rete or not provincia:
        return jsonify({"errore": "Servono rete e provincia."}), 400
    return jsonify(albo_ocf.colleghi_rimasti(rete, provincia))


@albo_bp.route("/albo/dossier", methods=["POST"])
@login_required
def dossier():
    """
    Dossier di UN consulente dell'albo. Il client lo chiama in progressivo per i
    profili scelti (uno alla volta, con poca concorrenza): ogni dossier fa una
    ricerca LinkedIn su Apify, che dura decine di secondi e si paga.
    """
    d = request.get_json() or {}
    profilo = d.get("profilo") or {}
    if not profilo.get("cognome"):
        return jsonify({"errore": "Profilo senza cognome."}), 400

    from services import dossier_albo
    try:
        esito = dossier_albo.costruisci(
            profilo,
            con_linkedin=d.get("con_linkedin", True),
            con_ai=d.get("con_ai", True),
        )
    except Exception as e:
        log.error("Dossier fallito per %s: %s", profilo.get("cognome"), e, exc_info=True)
        return jsonify({"errore": f"Dossier non riuscito: {e}"}), 500
    if not esito.get("ok"):
        return jsonify(esito), 400

    # Segna che questa persona è stata lavorata: alla prossima richiesta di
    # «primi 10» non deve ricomparire, altrimenti si ripaga la stessa ricerca.
    albo_ocf.registra_dossier(profilo,
                              linkedin_url=(esito.get("linkedin") or {}).get("url", ""),
                              esito="analizzato")
    return jsonify(esito)


def _testo_per_analisi(profilo: dict, linkedin: dict) -> str:
    """
    Testo su cui far ragionare l'AI: i dati dell'albo (certi) più quelli
    LinkedIn (se il profilo è stato confermato). Dichiara la provenienza di
    ciascun pezzo, così l'analisi non tratta un'ipotesi come un fatto.
    """
    linkedin = linkedin or {}
    parti = [
        f"Nome: {profilo.get('nome','')} {profilo.get('cognome','')}".strip(),
        f"Ruolo: {linkedin.get('ruolo') or 'Consulente finanziario'}",
        f"Azienda: {profilo.get('rete','')} (dato ufficiale albo OCF)",
        f"Location: {profilo.get('comune','')} ({profilo.get('provincia','')})",
    ]
    if profilo.get("eta"):
        parti.append(f"Età: {profilo['eta']} anni")
    if profilo.get("n_cambi"):
        parti.append(f"Passaggi di rete osservati negli elenchi ufficiali: {profilo['n_cambi']}")
    if linkedin.get("sommario"):
        parti.append(f"Sommario LinkedIn: {linkedin['sommario']}")
    if linkedin.get("url"):
        parti.append(f"LinkedIn: {linkedin['url']}")
    return "\n".join(parti)


@albo_bp.route("/albo/analizza", methods=["POST"])
@login_required
def analizza():
    """
    Analisi AI del profilo, come per gli altri candidati. Non salva nulla:
    restituisce il risultato perché venga mostrato nella scheda, e sarà
    l'utente a decidere se mandarlo in pipeline.
    """
    from ai_helpers import analizza_profilo_linkedin, messaggio_errore_ai

    d = request.get_json() or {}
    profilo = d.get("profilo") or {}
    tipo_profilo = d.get("tipo_profilo") or "A"
    if not profilo.get("cognome"):
        return jsonify({"errore": "Profilo senza cognome."}), 400

    db = get_db()
    try:
        imp = db.execute("SELECT * FROM impostazioni_profilo WHERE profilo = ?",
                         (tipo_profilo,)).fetchone()
    finally:
        db.close()

    testo = _testo_per_analisi(profilo, d.get("linkedin"))
    try:
        risultato = analizza_profilo_linkedin(testo, tipo_profilo, imp)
    except Exception as e:
        log.error("Analisi albo fallita: %s", e, exc_info=True)
        return jsonify({"errore": messaggio_errore_ai(e)}), 502

    return jsonify({"ok": True, "analisi": risultato, "testo_profilo": testo})


@albo_bp.route("/albo/in-pipeline", methods=["POST"])
@login_required
def in_pipeline():
    """
    Manda in pipeline un singolo consulente del dossier, con l'analisi se è
    stata fatta. Senza analisi il candidato entra comunque come «Da valutare»:
    i dati dell'albo sono verificati, non serve il parere dell'AI per salvarli.
    """
    import json as _json

    d = request.get_json() or {}
    profilo = d.get("profilo") or {}
    linkedin = d.get("linkedin") or {}
    analisi = d.get("analisi") or {}
    tipo_profilo = d.get("tipo_profilo") or "A"

    nome = (profilo.get("nome") or "").strip()
    cognome = (profilo.get("cognome") or "").strip()
    if not cognome:
        return jsonify({"errore": "Profilo senza cognome."}), 400

    url = linkedin.get("url", "")
    db = get_db()
    try:
        dup, motivo, cid = is_duplicate(db, {
            "nome": nome, "cognome": cognome,
            "azienda": profilo.get("rete", ""), "linkedin": url,
        })
        if dup:
            return jsonify({"duplicato": True, "motivo": motivo, "candidato_id": cid}), 409

        spunti = analisi.get("spunti_contatto") or []
        citta = ", ".join(x for x in [profilo.get("comune", ""), profilo.get("provincia", "")] if x)
        note = f"Da albo OCF · {citta}"
        prop = profilo.get("propensione") or {}
        if prop.get("indice"):
            note += f" · propensione {prop['indice']}x la media"
        gestore = ("Salvatore Sabia" if tipo_profilo == "A"
                   else "Firdaous Filahi" if tipo_profilo == "B" else "Non assegnato")

        cur = db.execute(
            """INSERT INTO candidati
               (nome, cognome, ruolo_attuale, azienda, note, profilo_linkedin,
                tipo_profilo, stato, punteggio, analisi, spunti, messaggio_outreach,
                source, url_fonte, gestore)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'ocf', ?, ?)""",
            (nome, cognome,
             linkedin.get("ruolo") or "Consulente finanziario",
             profilo.get("rete", ""), note, url, tipo_profilo,
             "Da contattare" if analisi else "Da valutare",
             analisi.get("punteggio"),
             analisi.get("analisi_percorso") or "",
             _json.dumps(spunti if isinstance(spunti, list) else [], ensure_ascii=False),
             analisi.get("messaggio_outreach") or "",
             url, gestore),
        )
        candidato_id = cur.lastrowid
        db.commit()
    except Exception as e:
        log.error("Invio in pipeline fallito: %s", e, exc_info=True)
        return jsonify({"errore": f"Salvataggio non riuscito: {e}"}), 500
    finally:
        db.close()

    albo_ocf.registra_dossier(profilo, linkedin_url=url, esito="in_pipeline",
                              punteggio=analisi.get("punteggio"), candidato_id=candidato_id)
    return jsonify({"ok": True, "candidato_id": candidato_id,
                    "con_analisi": bool(analisi)})


@albo_bp.route("/albo/movimenti")
@login_required
def lista_movimenti():
    return jsonify({"movimenti": albo_ocf.movimenti(
        tipo=request.args.get("tipo", ""),
        giorni=int(request.args.get("giorni", 365)),
        rete=request.args.get("rete", ""),
        provincia=request.args.get("provincia", ""),
    )})
