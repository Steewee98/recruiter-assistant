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

    def __init__(self, budget=None, sedi=None, ai_viva=None):
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
        }
        ai_helpers.test_connessione_api = lambda: self.ai_viva
        persone = _persone()
        prova = self

        # LinkedIn restituisce i tre profili (Bruno non c'è: simula chi su
        # LinkedIn non compare affatto)
        def raccolta(quanti):
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
        return False


def _ultimo_id_storico() -> int:
    """Id più alto in ocf_loop_run PRIMA del test: tutto ciò che nasce dopo è nostro."""
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
    del test e i nominativi inventati, nient'altro.
    """
    from database import get_db
    db = get_db()
    for c in COGNOMI_PROVA:
        db.execute("DELETE FROM candidati WHERE cognome = ?", (c,))
    db.execute("DELETE FROM ocf_dossier WHERE chiave IN ('loop_k1','loop_k2','loop_k3')")
    if da_id is not None:
        db.execute("DELETE FROM ocf_loop_run WHERE id > ?", (da_id,))
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
    assert not lavora_a_roma("Lazio, Italia"), "la sola regione non basta"
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
    L._raccogli_da_linkedin = lambda q: (ricerche_fatte.append(1), ([], 1, 0))[1]
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
    L._importa = lambda p, li, a: (None, "errore")
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
