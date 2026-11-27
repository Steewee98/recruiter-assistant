"""
Eval/guardrail del loop giornaliero.

Apify e AI sono simulati: il test non spende nulla e non dipende da nessun
fornitore esterno. Le righe create nel database vengono ripulite.

Esecuzione:
    venv/bin/python tests/test_loop_giornaliero.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

COGNOMI_PROVA = ("Loopuno", "Loopdue", "Looptre")


def _persone():
    """Tre profili come li restituisce albo_ocf.cerca()."""
    base = {"rete": "Azimut", "comune": "Roma", "provincia": "RM",
            "n_cambi": 0, "propensione": {"indice": 1.8}}
    return [
        dict(base, chiave="loop_k1", nome="Anna", cognome="Loopuno",
             nome_completo="Anna Loopuno", eta=44),
        dict(base, chiave="loop_k2", nome="Bruno", cognome="Loopdue",
             nome_completo="Bruno Loopdue", eta=50),
        dict(base, chiave="loop_k3", nome="Carla", cognome="Looptre",
             nome_completo="Carla Looptre", eta=33),
    ]


class _finto_mondo:
    """
    Sostituisce ricerca albo, ricerca LinkedIn e analisi AI per la durata del
    blocco, e ripristina tutto all'uscita.

    - Anna  → su LinkedIn c'è, punteggio 9 → deve entrare in pipeline
    - Bruno → su LinkedIn non compare       → non arriva nemmeno al giro
    - Carla → su LinkedIn c'è, punteggio 4  → scartata dopo l'analisi
    """

    def __init__(self, budget=None, sedi=None, ai_viva=None, acceso=True):
        self.acceso = acceso
        self.budget = budget or {"noto": True, "usato": 1.0, "tetto": 29.0, "residuo": 28.0}
        self.sedi = sedi or {}
        self.ai_viva = ai_viva or {"ok": True}
        self.analisi_fatte = []

    def __enter__(self):
        from services import albo_ocf, dossier_albo, loop_giornaliero
        import ai_helpers

        self._orig = {
            "raccolta": loop_giornaliero._raccogli_da_linkedin,
            "ai": ai_helpers.analizza_profilo_linkedin,
            "budget": loop_giornaliero.budget_apify,
            "ping": ai_helpers.test_connessione_api,
            "attivo": loop_giornaliero.attivo,
        }
        # L'interruttore vero sta nel database di produzione: il test non lo
        # legge né lo tocca, così non dipende da come l'ha lasciato Stefano.
        loop_giornaliero.attivo = lambda: prova_acceso
        prova_acceso = self.acceso
        ai_helpers.test_connessione_api = lambda: self.ai_viva
        persone = _persone()
        prova = self

        # LinkedIn restituisce i tre profili (Bruno non c'è: simula chi su
        # LinkedIn non compare affatto)
        def raccolta(quanti, bersaglio=None):
            fuori = []
            for p in persone:
                if p["cognome"] == "Loopdue":
                    continue
                q = dict(p)
                q["_linkedin"] = {
                    "nome": p["nome"], "cognome": p["cognome"],
                    "ruolo": "Consulente finanziario presso Azimut",
                    "azienda": "Azimut", "sommario": "esperienza",
                    "location": prova.sedi.get(p["cognome"], "Rome, Latium, Italy"),
                    "linkedin": f"https://linkedin.com/in/{p['chiave']}"}
                fuori.append(q)
            return fuori[:quanti], 1, 25
        loop_giornaliero._raccogli_da_linkedin = raccolta

        prova = self

        def ai(testo, tipo, imp=None):
            prova.analisi_fatte.append((testo, tipo))
            punteggio = 4 if "Carla" in testo else 9
            return {"punteggio": punteggio, "analisi_percorso": "analisi finta",
                    "spunti_contatto": ["spunto"], "messaggio_outreach": "ciao"}
        ai_helpers.analizza_profilo_linkedin = ai
        loop_giornaliero.budget_apify = lambda: prova.budget
        return self

    def __exit__(self, *e):
        from services import albo_ocf, dossier_albo, loop_giornaliero
        import ai_helpers
        loop_giornaliero._raccogli_da_linkedin = self._orig["raccolta"]
        ai_helpers.test_connessione_api = self._orig["ping"]
        ai_helpers.analizza_profilo_linkedin = self._orig["ai"]
        loop_giornaliero.budget_apify = self._orig["budget"]
        loop_giornaliero.attivo = self._orig["attivo"]
        return False


# Id delle righe di ocf_loop_run scritte DAI TEST: il giro vero può girare
# nello stesso momento sullo stesso database, e le sue righe non si toccano.
RUN_DEL_TEST = []


def _traccia_run_del_test():
    from services import loop_giornaliero as L
    if getattr(L._registra_run, "_tracciato", False):
        return
    originale = L._registra_run

    def registra(esito, stato):
        nuovo = originale(esito, stato)
        if nuovo:
            RUN_DEL_TEST.append(nuovo)
        return nuovo
    registra._tracciato = True
    L._registra_run = registra


def _ultimo_id_storico() -> int:
    """Id più alto in ocf_loop_run prima del test (oggi serve solo ad attivare la traccia)."""
    _traccia_run_del_test()
    from database import get_db
    db = get_db()
    try:
        r = db.execute("SELECT COALESCE(MAX(id), 0) AS m FROM ocf_loop_run").fetchone()
        return (r or {}).get("m", 0) or 0
    finally:
        db.close()


def _pulisci(da_id: int = None):
    """
    Rimuove SOLO ciò che il test ha creato.

    I test girano sullo stesso database dell'applicazione (il progetto non ne ha
    uno separato), quindi la pulizia dev'essere chirurgica: cancellare per
    somiglianza di testo — «tutte le righe che contengono Loop», «la nota che
    cita 28.5» — rischia di portarsi via storico vero, perché una nota reale
    può benissimo contenere quel numero. Si cancellano gli id nati dopo l'inizio
    del test e i nominativi inventati, nient'altro: per ocf_loop_run solo gli
    id che il test stesso ha registrato, mai «tutto ciò che è dopo un id».
    """
    from database import get_db
    db = get_db()
    for c in COGNOMI_PROVA:
        db.execute("DELETE FROM candidati WHERE cognome = ?", (c,))
    db.execute("DELETE FROM ocf_dossier WHERE chiave IN ('loop_k1','loop_k2','loop_k3')")
    while RUN_DEL_TEST:
        db.execute("DELETE FROM ocf_loop_run WHERE id = ?", (RUN_DEL_TEST.pop(),))
    db.commit()
    db.close()


def test_giro_completo_importa_solo_chi_supera_la_soglia():
    from database import get_db
    from services import loop_giornaliero as L

    da_id = _ultimo_id_storico()
    _pulisci(da_id)
    try:
        with _finto_mondo() as mondo:
            r = L.esegui(limite=3)

        assert r["ok"], r
        assert r["esaminati"] == 2, "solo chi ha un profilo LinkedIn entra nel giro"
        assert r["con_linkedin"] == 2
        assert r["analizzati"] == 2, "l'AI non deve girare su chi non ha LinkedIn"
        assert r["importati"] == 1, "solo Anna supera la soglia"
        assert r["scartati_punteggio"] == 1

        # Non si paga l'analisi di chi è già stato escluso prima
        assert len(mondo.analisi_fatte) == 2
        assert all("Bruno" not in t for t, _ in mondo.analisi_fatte)

        # Il tipo profilo segue l'età: Carla ha 33 anni → profilo B
        tipi = {t: tipo for t, tipo in mondo.analisi_fatte}
        assert any(tipo == "B" for t, tipo in mondo.analisi_fatte if "Carla" in t)
        assert any(tipo == "A" for t, tipo in mondo.analisi_fatte if "Anna" in t)

        db = get_db()
        riga = db.execute("SELECT * FROM candidati WHERE cognome = 'Loopuno'").fetchone()
        db.close()
        assert riga is not None, "Anna doveva entrare in pipeline"
        assert riga["stato"] == "Da valutare", "la scelta l'ha fatta una macchina: si valuta"
        assert riga["punteggio"] == 9
        assert riga["source"] == "ocf"
        assert "loop giornaliero" in (riga["note"] or "")
    finally:
        _pulisci(da_id)


def test_ogni_persona_toccata_viene_registrata():
    """Anche gli scarti: senza traccia domani si ripagherebbe la stessa ricerca."""
    from database import get_db
    from services import loop_giornaliero as L

    da_id = _ultimo_id_storico()
    _pulisci(da_id)
    try:
        with _finto_mondo():
            L.esegui(limite=3)
        db = get_db()
        righe = {r["chiave"]: r["esito"] for r in db.execute(
            "SELECT chiave, esito FROM ocf_dossier WHERE chiave LIKE 'loop_k%'").fetchall()}
        db.close()
        assert righe.get("loop_k1") == "in_pipeline"
        assert righe.get("loop_k3") == "scartato_punteggio"
        assert "loop_k2" not in righe, "chi non compare su LinkedIn non viene nemmeno toccato"
    finally:
        _pulisci(da_id)


def test_si_ferma_se_il_budget_e_finito():
    """Il freno che evita di consumare i soldi delle ricerche a mano."""
    from services import loop_giornaliero as L

    da_id = _ultimo_id_storico()
    _pulisci(da_id)
    try:
        with _finto_mondo(budget={"noto": True, "usato": 28.5, "tetto": 29.0,
                                  "residuo": 0.5, "fine_ciclo": "2026-09-18"}) as mondo:
            r = L.esegui(limite=3)
        assert not r["ok"]
        assert r["esaminati"] == 0, "non deve nemmeno cercare"
        assert "budget" in r["nota"].lower()
        assert not mondo.analisi_fatte, "nessuna chiamata AI a budget esaurito"
    finally:
        _pulisci(da_id)


def test_budget_non_leggibile_non_blocca():
    """Se Apify non risponde sul budget non ci si ferma: si prova, con cautela."""
    from services import loop_giornaliero as L

    da_id = _ultimo_id_storico()
    _pulisci(da_id)
    try:
        with _finto_mondo(budget={"noto": False, "motivo": "rete giù"}):
            r = L.esegui(limite=3)
        assert r["ok"] and r["esaminati"] == 2
    finally:
        _pulisci(da_id)


def test_storico_registrato():
    from database import get_db
    from services import loop_giornaliero as L

    da_id = _ultimo_id_storico()
    _pulisci(da_id)
    try:
        with _finto_mondo():
            L.esegui(limite=3)
        ultime = L.ultime_esecuzioni(1)
        assert ultime and ultime[0]["stato"] == "completata"
        assert ultime[0]["importati"] == 1 and ultime[0]["scartati_punteggio"] == 1
        assert "Loopuno" in (ultime[0]["dettaglio"] or ""), "il dettaglio deve dire chi e perché"
        assert ultime[0]["id"] > da_id, "la riga dev'essere nostra, non storico preesistente"
    finally:
        _pulisci(da_id)


def test_scarta_chi_su_linkedin_non_e_a_roma():
    """
    L'albo dà il domicilio, LinkedIn la sede di lavoro: se la sede non è Roma il
    candidato non è del territorio e non va importato — né analizzato, perché
    l'analisi si paga.
    """
    from services import loop_giornaliero as L

    da_id = _ultimo_id_storico()
    _pulisci(da_id)
    try:
        with _finto_mondo(sedi={"Loopuno": "Milan, Lombardy, Italy",
                                "Looptre": ""}) as mondo:
            r = L.esegui(limite=3)
        assert r["fuori_zona"] == 2, r
        assert r["importati"] == 0, "nessuno dei due lavora a Roma"
        assert not mondo.analisi_fatte, "non si analizza chi è gia' fuori zona"
    finally:
        _pulisci(da_id)


def test_riconoscimento_sede_romana():
    from services.loop_giornaliero import lavora_a_roma
    assert lavora_a_roma("Rome, Latium, Italy")
    assert lavora_a_roma("Roma, Lazio, Italia")
    assert lavora_a_roma("Rome Metropolitan Area")
    assert lavora_a_roma("Frascati, Lazio, Italy"), "i comuni della provincia valgono"
    assert not lavora_a_roma("Milan, Lombardy, Italy")
    assert lavora_a_roma("Lazio, Italia"), "dal 25/11 la zona è tutto il Lazio"
    assert lavora_a_roma("Latina, Lazio, Italy"), "le province intorno valgono"
    assert lavora_a_roma("Viterbo, Italy")
    assert not lavora_a_roma("Terni, Umbria, Italy"), "fuori dal Lazio no"
    assert not lavora_a_roma(""), "sede vuota non è una conferma"
    assert not lavora_a_roma(None)


def test_non_spende_in_ricerche_se_lai_e_giu():
    """
    Il caso costato davvero 0,30 $: dieci candidati trovati e nessuno
    analizzabile perché il credito AI era finito. Ora si verifica prima.
    """
    from services import loop_giornaliero as L

    da_id = _ultimo_id_storico()
    _pulisci(da_id)
    ricerche_fatte = []
    originale = L._raccogli_da_linkedin
    L._raccogli_da_linkedin = lambda q, *b: (ricerche_fatte.append(1), ([], 1, 0))[1]
    try:
        with _finto_mondo(ai_viva={"ok": False, "errore": "credit balance is too low"}):
            r = L.esegui(limite=10)
        assert not r["ok"]
        assert "AI non risponde" in r["nota"], r["nota"]
        assert not ricerche_fatte, "non deve partire nessuna ricerca a pagamento"
    finally:
        L._raccogli_da_linkedin = originale
        _pulisci(da_id)


def test_errore_di_salvataggio_non_marca_il_candidato_come_gia_presente():
    """
    Un INSERT fallito non è un duplicato: se lo si tratta come tale, la persona
    resta segnata come lavorata e non torna mai più, senza essere mai entrata.
    """
    from database import get_db
    from services import loop_giornaliero as L

    da_id = _ultimo_id_storico()
    _pulisci(da_id)
    originale = L._importa
    L._importa = lambda p, li, a, *b: (None, "errore")
    try:
        with _finto_mondo():
            r = L.esegui(limite=3)
        assert r["importati"] == 0
        assert r["errori"] >= 1, r
        db = get_db()
        righe = db.execute(
            "SELECT chiave FROM ocf_dossier WHERE chiave LIKE 'loop_k%'").fetchall()
        db.close()
        chiavi = {x["chiave"] for x in righe}
        assert "loop_k1" not in chiavi, "chi non è stato salvato dev'essere ritentabile domani"
    finally:
        L._importa = originale
        _pulisci(da_id)


def test_sede_fuori_italia_o_regione_omonima_non_passa():
    """Il difetto trovato dalla revisione: «roma» dentro «Emilia-Romagna»."""
    from services.loop_giornaliero import lavora_a_roma
    assert not lavora_a_roma("Bologna, Emilia-Romagna, Italy")
    assert not lavora_a_roma("Forlì, Emilia-Romagna")
    assert not lavora_a_roma("Bucharest, Romania")
    assert not lavora_a_roma("San Marino")
    assert not lavora_a_roma("Rome, New York, United States"), "Roma sbagliata"
    assert lavora_a_roma("Greater Rome Metropolitan Area")
    assert lavora_a_roma("Fiumicino, Lazio, Italy")


def test_un_giro_saltato_non_consuma_la_giornata():
    """
    Se alle 6 del mattino manca il credito AI, il giro viene saltato: non deve
    valere come «fatto per oggi», altrimenti si perde l'intera giornata e ce ne
    si accorge domani.
    """
    from database import get_db
    from services import loop_giornaliero as L

    def quanti_contano():
        """Giri di oggi che consumano la giornata (compresi quelli veri già in archivio)."""
        db = get_db()
        try:
            segnaposto = ",".join(["?"] * len(L.ESITI_CHE_CONTANO))
            return db.execute(
                f"""SELECT COUNT(*) AS n FROM ocf_loop_run
                     WHERE DATE(eseguito_il) = CURRENT_DATE AND stato IN ({segnaposto})""",
                list(L.ESITI_CHE_CONTANO)).fetchone()["n"]
        finally:
            db.close()

    da_id = _ultimo_id_storico()
    _pulisci(da_id)
    try:
        # Il conteggio parte da quello che c'è già: il test non deve dipendere
        # dal fatto che oggi sia stato eseguito un giro vero.
        base = quanti_contano()

        with _finto_mondo(ai_viva={"ok": False, "errore": "credit balance is too low"}):
            L.esegui(limite=3)
        assert quanti_contano() == base, "un giro saltato per AI giù non consuma la giornata"

        with _finto_mondo(budget={"noto": True, "usato": 34.8, "tetto": 35.0,
                                  "residuo": 0.2, "fine_ciclo": "2026-09-18"}):
            L.esegui(limite=3)
        assert quanti_contano() == base, "nemmeno un giro saltato per budget"

        with _finto_mondo():
            L.esegui(limite=3)
        assert quanti_contano() == base + 1, "un giro completato invece sì"
        assert L.gia_eseguito_oggi()
    finally:
        _pulisci(da_id)


def test_interruttore_spento_ferma_il_giro_automatico():
    """
    Spento vuol dire spento: nessuna ricerca, nessuna analisi, e la giornata NON
    viene consumata — così riaccendendolo il giro di oggi è ancora disponibile.
    """
    from database import get_db
    from services import loop_giornaliero as L

    def giri_che_contano():
        """Giri di oggi che consumano la giornata, compresi quelli veri già in archivio."""
        db = get_db()
        try:
            segnaposto = ",".join(["?"] * len(L.ESITI_CHE_CONTANO))
            return db.execute(
                f"""SELECT COUNT(*) AS n FROM ocf_loop_run
                     WHERE DATE(eseguito_il) = CURRENT_DATE AND stato IN ({segnaposto})""",
                list(L.ESITI_CHE_CONTANO)).fetchone()["n"]
        finally:
            db.close()

    da_id = _ultimo_id_storico()
    _pulisci(da_id)
    ricerche_fatte = []
    originale = L._raccogli_da_linkedin
    L._raccogli_da_linkedin = lambda q, *b: (ricerche_fatte.append(1), ([], 1, 0))[1]
    try:
        # Il conteggio parte da quello che c'è già: oggi può esserci un giro vero.
        base = giri_che_contano()
        with _finto_mondo(acceso=False) as mondo:
            r = L.esegui(limite=3)
        assert not r["ok"]
        assert "spento" in r["nota"].lower(), r["nota"]
        assert not ricerche_fatte, "a interruttore spento non parte nessuna ricerca a pagamento"
        assert not mondo.analisi_fatte, "né alcuna analisi AI"
        assert giri_che_contano() == base, "un giro saltato perché spento non consuma la giornata"

        db = get_db()
        ultima = db.execute("SELECT stato FROM ocf_loop_run ORDER BY id DESC LIMIT 1").fetchone()
        db.close()
        assert ultima["stato"] == "saltata_spento", ultima
    finally:
        L._raccogli_da_linkedin = originale
        _pulisci(da_id)


def test_bottone_manuale_funziona_anche_a_interruttore_spento():
    """
    Spento significa «non partire da solo», non «sei disabilitato»: se una persona
    chiede esplicitamente un giro, il giro si fa.
    """
    from services import loop_giornaliero as L

    da_id = _ultimo_id_storico()
    _pulisci(da_id)
    try:
        with _finto_mondo(acceso=False):
            r = L.esegui(limite=3, forzato=True)
        assert r["ok"], r
        assert r["importati"] == 1, r
    finally:
        _pulisci(da_id)


def test_interruttore_si_ricorda_lo_stato():
    """Acceso o spento deve sopravvivere al riavvio: sta in database, non in memoria."""
    from services import loop_giornaliero as L

    prima = L.attivo()
    try:
        L.imposta_attivo(True)
        assert L.attivo() is True
        L.imposta_attivo(False)
        assert L.attivo() is False
    finally:
        L.imposta_attivo(prima)


def test_due_notti_di_fila_non_ripetono_le_stesse_ricerche():
    """
    Il bug del 3-4/11: tre ricerche su quattro erano quelle della notte prima.
    Su un anno intero di giorni, nessuna coppia (ruolo, pagina) deve tornare
    prima che siano state usate tutte le altre.
    """
    from datetime import date
    from services import loop_giornaliero as L

    totale = len(L.RUOLI_LINKEDIN) * len(L.PARTENZE)
    notti_senza_ripetere = totale // L.MAX_RICERCHE
    inizio = date(2026, 1, 1).toordinal()
    for g in range(inizio, inizio + 366):
        finestra = [c for d in range(g, g + notti_senza_ripetere)
                    for c in L.ricerche_del_giorno(d)]
        assert len(finestra) == len(set(finestra)), f"ripetizione entro {notti_senza_ripetere} notti dal giorno {g}"


def test_la_rotazione_copre_tutte_le_combinazioni():
    from services import loop_giornaliero as L

    attese = {(r, p) for p in L.PARTENZE for r in L.RUOLI_LINKEDIN}
    viste = {c for g in range(800000, 800000 + len(attese)) for c in L.ricerche_del_giorno(g)}
    assert viste == attese


def test_ogni_ricerca_chiede_tre_pagine_e_partenze_diverse():
    """Una pagina LinkedIn ha 10 profili: con una sola si pagavano 25 e se ne ricevevano 10."""
    import routes.ricerca as R
    from services import loop_giornaliero as L

    chiamate = []
    orig_cerca, orig_albo = R.cerca_apify, L._cerca_nellalbo
    R.cerca_apify = lambda **kw: (chiamate.append(kw), ([], None))[1]
    L._cerca_nellalbo = lambda n, c, *b: None
    try:
        _, ricerche, _ = L._raccogli_da_linkedin(10)
    finally:
        R.cerca_apify, L._cerca_nellalbo = orig_cerca, orig_albo
    assert ricerche == L.MAX_RICERCHE == len(chiamate)
    assert all(k["num_pagine"] == L.PAGINE_PER_RICERCA for k in chiamate)
    assert len({(k["ruolo"], k["start_page"]) for k in chiamate}) == len(chiamate)


def test_se_tutte_le_ricerche_falliscono_la_notte_e_un_errore():
    """Dal 12/11 la chiave Apify scaduta risultava «0 profili, nulla da fare»."""
    import routes.ricerca as R
    from services import loop_giornaliero as L

    orig = R.cerca_apify
    R.cerca_apify = lambda **kw: (None, "Errore avvio ricerca Apify: 401")
    try:
        L._raccogli_da_linkedin(10)
        raise AssertionError("doveva sollevare un errore")
    except RuntimeError as e:
        assert "401" in str(e)
    finally:
        R.cerca_apify = orig


def test_una_ricerca_fallita_su_quattro_non_ferma_la_notte():
    import routes.ricerca as R
    from services import loop_giornaliero as L

    risposte = iter([(None, "timeout"), ([], None), ([], None), ([], None)])
    orig = R.cerca_apify
    R.cerca_apify = lambda **kw: next(risposte)
    try:
        _, ricerche, visti = L._raccogli_da_linkedin(10)
    finally:
        R.cerca_apify = orig
    assert ricerche == 4 and visti == 0


def test_apify_legge_tutti_i_profili_chiesti_e_la_chiave_non_va_nell_indirizzo():
    """Il dataset si leggeva con limit=10: chiedere 25 profili non serviva a niente."""
    import routes.ricerca as R

    chiamate = []

    class Risposta:
        def __init__(self, dati): self.dati = dati
        def raise_for_status(self): pass
        def json(self): return self.dati

    def finto_post(url, **kw):
        chiamate.append((url, kw))
        return Risposta({"data": {"id": "run1", "defaultDatasetId": "ds1"}})

    def finto_get(url, **kw):
        chiamate.append((url, kw))
        if "actor-runs" in url:
            return Risposta({"data": {"status": "SUCCEEDED", "defaultDatasetId": "ds1"}})
        return Risposta([{"fullName": f"P{i}"} for i in range(25)])

    orig = (R.requests.post, R.requests.get, R.time.sleep)
    R.requests.post, R.requests.get, R.time.sleep = finto_post, finto_get, lambda s: None
    try:
        items, errore = R._esegui_run_apify({}, "CHIAVE-SEGRETA", max_wait=10, max_items=25)
    finally:
        R.requests.post, R.requests.get, R.time.sleep = orig
    assert errore is None and len(items) == 25
    lettura = [kw for url, kw in chiamate if "/datasets/" in url][0]
    assert lettura["params"]["limit"] == 25
    for url, kw in chiamate:
        assert "CHIAVE-SEGRETA" not in url
        assert "CHIAVE-SEGRETA" not in str(kw.get("params", ""))
        assert kw["headers"]["Authorization"] == "Bearer CHIAVE-SEGRETA"


# ── Giro di Firdaous (dal 25/11/2026) ────────────────────────────────────────
# Prove senza database: controllano i criteri, non i dati.

class _DbFinto:
    """Registra la query di _cerca_nellalbo invece di eseguirla."""
    def __init__(self):
        self.sql, self.valori = "", []

    def execute(self, sql, valori=()):
        self.sql, self.valori = sql, list(valori)
        return self

    def fetchone(self):
        return None

    def close(self):
        pass


def _query_albo(bersaglio):
    from services import loop_giornaliero as L
    db = _DbFinto()
    orig = L.get_db
    L.get_db = lambda: db
    try:
        L._cerca_nellalbo("Anna", "Rossi", bersaglio)
    finally:
        L.get_db = orig
    return db


def test_firdaous_cerca_solo_under_45_con_mandato_di_una_banca():
    from datetime import date
    from services import loop_giornaliero as L

    db = _query_albo(L.FIRDAOUS)
    anno = date.today().year
    assert "anno_nascita >= ?" in db.sql and (anno - 44) in db.valori
    assert "anno_nascita <= ?" not in db.sql          # nessuna età minima
    assert "rete IN (" in db.sql
    for banca in L.BANCHE_CON_SPORTELLI:
        assert banca in db.valori
    assert db.sql.count("?") == len(db.valori)        # segnaposto e valori allineati


def test_il_gruppo_fideuram_e_escluso_da_entrambi_i_giri():
    """Intesa Sanpaolo è lo stesso gruppo di Fideuram (Stefano, 25/11/2026)."""
    from services import loop_giornaliero as L
    from services.albo_ocf import GRUPPO_PROPRIO
    assert {"Fideuram", "Intesa Sanpaolo", "Intesa Sanpaolo Private Banking"} <= set(GRUPPO_PROPRIO)
    assert not set(GRUPPO_PROPRIO) & L.BANCHE_CON_SPORTELLI
    for b in (L.SABIA, L.FIRDAOUS):
        db = _query_albo(b)
        assert "rete NOT IN (" in db.sql
        assert all(g in db.valori for g in GRUPPO_PROPRIO)
        assert db.sql.count("?") == len(db.valori)


def test_il_giro_di_sabia_non_cambia():
    """Stessi criteri di prima del 25/11: sopra i 30 anni, qualunque mandato tranne Fideuram."""
    from datetime import date
    from services import loop_giornaliero as L

    db = _query_albo(L.SABIA)
    assert "anno_nascita <= ?" in db.sql and (date.today().year - L.ETA_MINIMA) in db.valori
    assert "anno_nascita >= ?" not in db.sql and "rete IN (" not in db.sql
    assert db.sql.count("?") == len(db.valori)
    assert L.ricerche_del_giorno(800001) == L.ricerche_del_giorno(800001, L.RUOLI_LINKEDIN)


def test_le_ricerche_di_firdaous_usano_solo_i_suoi_ruoli():
    from services import loop_giornaliero as L
    viste = {r for g in range(800000, 800050) for r, _ in L.ricerche_del_giorno(g, L.FIRDAOUS.ruoli)}
    assert viste == set(L.RUOLI_FIRDAOUS)


def test_gestore_e_profilo_per_bersaglio():
    from services import loop_giornaliero as L
    # Firdaous: sempre profilo B e sempre sua, anche se l'età mancasse
    for eta in (None, 25, 34):
        t = L._tipo({"eta": eta}, L.FIRDAOUS)
        assert t == "B" and L._gestore(t, L.FIRDAOUS) == "Firdaous Filahi"
    # Sabia: la regola di sempre, decide l'età
    assert L._gestore(L._tipo({"eta": 50}, L.SABIA), L.SABIA) == "Salvatore Sabia"
    assert L._gestore(L._tipo({"eta": 32}, L.SABIA), L.SABIA) == "Firdaous Filahi"


def test_i_due_giri_hanno_lucchetti_diversi():
    """Con lo stesso lucchetto il secondo giro risulterebbe «già in corso» e salterebbe."""
    from services import loop_giornaliero as L
    assert len({b.lucchetto for b in L.BERSAGLI.values()}) == len(L.BERSAGLI)
    assert len({b.codice for b in L.BERSAGLI.values()}) == len(L.BERSAGLI)


def test_la_raccolta_passa_il_bersaglio_all_albo():
    import routes.ricerca as R
    from services import loop_giornaliero as L

    visti_bersagli, ruoli = [], []
    orig_cerca, orig_albo = R.cerca_apify, L._cerca_nellalbo
    R.cerca_apify = lambda **kw: (ruoli.append(kw["ruolo"]),
                                  ([{"firstName": "Anna", "lastName": "Rossi"}], None))[1]
    L._cerca_nellalbo = lambda n, c, b=None: (visti_bersagli.append(b), None)[1]
    try:
        L._raccogli_da_linkedin(10, L.FIRDAOUS)
    finally:
        R.cerca_apify, L._cerca_nellalbo = orig_cerca, orig_albo
    assert visti_bersagli and all(b is L.FIRDAOUS for b in visti_bersagli)
    assert ruoli and set(ruoli) <= set(L.RUOLI_FIRDAOUS)


def test_entrambi_i_giri_cercano_nelle_province_intorno_a_roma():
    """Richiesta di Stefano (25/11/2026): Roma e province intorno, per tutti e due."""
    from services import loop_giornaliero as L
    assert set(L.ZONA) == {"RM", "LT", "FR", "VT", "RI"}
    for b in (L.SABIA, L.FIRDAOUS):
        db = _query_albo(b)
        assert "provincia IN (" in db.sql
        assert all(p in db.valori for p in L.ZONA)
        assert db.sql.count("?") == len(db.valori)


def test_una_ricerca_al_giorno_nei_capoluoghi_intorno():
    from services import loop_giornaliero as L
    for g in range(800000, 800020):
        citta = [L.citta_della_ricerca(g, i) for i in range(L.MAX_RICERCHE)]
        assert citta[:-1] == ["Roma"] * (L.MAX_RICERCHE - 1), "Roma resta la fonte principale"
        assert citta[-1] in L.CAPOLUOGHI_INTORNO
    ultime = {L.citta_della_ricerca(g, L.MAX_RICERCHE - 1) for g in range(800000, 800008)}
    assert ultime == set(L.CAPOLUOGHI_INTORNO), "in pochi giorni li gira tutti"


def test_firdaous_entra_da_6_sabia_resta_a_7():
    """Stefano, 27/11/2026: per Firdaous vanno bene anche i voti 6."""
    from services import loop_giornaliero as L
    assert L.FIRDAOUS.punteggio_minimo == 6
    assert L.SABIA.punteggio_minimo == L.PUNTEGGIO_MINIMO == 7


def test_firdaous_rivede_i_6_scartati_da_sabia():
    from services import loop_giornaliero as L
    for b, soglia in ((L.FIRDAOUS, 6), (L.SABIA, 7)):
        db = _query_albo(b)
        assert "esito = 'scartato_punteggio'" in db.sql and "COALESCE(punteggio, 0) >= ?" in db.sql
        assert db.valori[-(len(L.ZONA) + 3)] == soglia
        assert db.sql.count("?") == len(db.valori)


def test_gli_omonimi_nella_zona_si_saltano():
    from services import loop_giornaliero as L
    db = _query_albo(L.FIRDAOUS)
    assert "SELECT COUNT(*) FROM ocf_iscritti o" in db.sql and ") = 1" in db.sql
    assert db.valori[-2:] == ["Anna", "Rossi"]
    assert db.sql.count("?") == len(db.valori)


if __name__ == "__main__":
    import types
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))
    import logging
    logging.getLogger("database").setLevel(logging.ERROR)
    logging.getLogger("services.loop_giornaliero").setLevel(logging.ERROR)

    passati, falliti = 0, 0
    for nome, fn in sorted(globals().items()):
        if not (nome.startswith("test_") and isinstance(fn, types.FunctionType)):
            continue
        try:
            fn()
            print(f"  ✓ {nome}")
            passati += 1
        except Exception as e:
            print(f"  ✗ {nome}: {type(e).__name__}: {e}")
            falliti += 1
    print(f"\n{passati} passati, {falliti} falliti")
    sys.exit(1 if falliti else 0)
