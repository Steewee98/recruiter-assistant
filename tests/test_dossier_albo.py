"""
Eval/guardrail per il dossier dell'albo.

Tutto offline: la ricerca LinkedIn e l'AI sono sostituite da finti, quindi
nessuna chiamata Apify e nessun costo.

Esecuzione:
    venv/bin/python tests/test_dossier_albo.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PROFILO = {"nome": "Mario", "cognome": "Rossi", "rete": "BNL BNP Paribas",
           "comune": "Roma", "provincia": "RM", "eta": 40, "n_cambi": 0}


def _finto_apify(items):
    def finto(ruolo, citta="", paese="", azienda="", parole_chiave="", num_pagine=1,
              ruoli_lista=None, forza_italia=True, progress_cb=None, start_page=1,
              max_items=10, max_wait=180, cerca_nome=None):
        return items, None
    return finto


class _con_apify:
    """
    Sostituisce la ricerca Apify per la durata del blocco e la ripristina sempre.
    Senza ripristino la sostituzione resterebbe attiva per i test successivi
    eseguiti nello stesso processo.
    """

    def __init__(self, items=None, errore=None):
        self.items, self.errore = items, errore

    def __enter__(self):
        import routes.ricerca as R
        self._originale = R.cerca_apify
        if self.errore:
            def rotto(*a, **k):
                return None, self.errore
            R.cerca_apify = rotto
        else:
            R.cerca_apify = _finto_apify(self.items)
        return self

    def __exit__(self, *e):
        import routes.ricerca as R
        R.cerca_apify = self._originale
        return False


class _senza_ai:
    """Simula l'API Anthropic non disponibile, ripristinandola all'uscita."""

    def __enter__(self):
        import ai_helpers
        self._originale = ai_helpers._chiama_api

        def esplode(*a, **k):
            raise RuntimeError("credito esaurito")
        ai_helpers._chiama_api = esplode
        return self

    def __exit__(self, *e):
        import ai_helpers
        ai_helpers._chiama_api = self._originale
        return False


def test_scarta_chi_non_ha_il_nome_giusto():
    from services import dossier_albo
    with _con_apify([{"firstName": "Luca", "lastName": "Bianchi", "headline": "Private Banker",
                      "linkedinUrl": "https://linkedin.com/in/luca"}]):
        p, nota, altri = dossier_albo._cerca_linkedin("Mario", "Rossi", "BNL BNP Paribas", "Roma")
    assert p is None, "un omonimo sbagliato non deve finire nel dossier"
    assert "Nessuna corrispondenza" in nota and "Luca Bianchi" in nota


def test_nome_simile_non_e_lo_stesso_nome():
    """«Mario Rossi» non deve accettare «Gianmario Rossini»: il confronto va per parole intere."""
    from services import dossier_albo
    assert dossier_albo._nome_uguale("Mario", "Rossi", "Mario", "Rossi")
    assert dossier_albo._nome_uguale("Mario", "Rossi", "Mario Luigi", "Rossi")
    assert dossier_albo._nome_uguale("Nicolo'", "D'Alo", "Nicolò", "D Alò"), "accenti e apostrofi"
    assert not dossier_albo._nome_uguale("Mario", "Rossi", "Gianmario", "Rossini")
    assert not dossier_albo._nome_uguale("Mario", "Rossi", "Mario", "Rossini")
    assert not dossier_albo._nome_uguale("Mario", "Rossi", "Gianmario", "Rossi")
    assert not dossier_albo._nome_uguale("Mario", "", "Mario", "Rossi"), "senza cognome non si conferma"
    # Un cognome più lungo è un'altra persona: l'albo riporta quello legale completo
    assert not dossier_albo._nome_uguale("Mario", "Rossi", "Mario", "Rossi Bianchi")
    assert dossier_albo._nome_uguale("Mario", "Rossi Bianchi", "Mario", "Rossi Bianchi")
    # Sigle professionali nel campo nome non devono far scartare la persona giusta
    assert dossier_albo._nome_uguale("Mario", "Rossi", "Mario", "Rossi EFPA")
    assert dossier_albo._nome_uguale("Mario", "Rossi", "Mario CFA", "Rossi")
    # Nominativo tutto in un campo solo (risposte con fullName)
    assert dossier_albo._nome_uguale("Mario", "Rossi", "Mario Rossi", "")


def test_sintesi_non_usa_un_profilo_non_confermato():
    """
    Se il profilo LinkedIn è marcato come dubbio, l'AI non deve riceverlo:
    scriverebbe argomenti riferiti a un omonimo.
    """
    from services import dossier_albo

    catturato = {}

    import ai_helpers
    originale = ai_helpers._chiama_api

    class _Finta:
        content = [type("T", (), {"text": "ok"})()]

    def finta(funzione, payload):
        catturato["prompt"] = payload["messages"][0]["content"]
        catturato["system"] = payload.get("system", "")
        return _Finta()

    ai_helpers._chiama_api = finta
    try:
        dubbio = {
            "nome_completo": "Mario Rossi",
            "albo": {"rete": "BNL BNP Paribas", "comune": "Roma", "provincia": "RM",
                     "eta": 40, "n_cambi": 0},
            "propensione": {"disponibile": True, "indice": 2.0, "probabilita_annua": 3.0,
                            "perche": ["rete mobile"]},
            "linkedin": {"ruolo": "HR Specialist", "azienda": "Acme", "sommario": "risorse umane"},
            "contesto_rete": [],
            "note": ["8 profili con questo nome e nessuno che risulti del settore "
                     "finanziario: da verificare a mano prima di usarlo."],
        }
        dossier_albo._sintesi_ai(dubbio)
        assert "HR Specialist" not in catturato["prompt"], catturato["prompt"]
        assert "NON confermato" in catturato["prompt"]

        sicuro = dict(dubbio, note=[])
        dossier_albo._sintesi_ai(sicuro)
        assert "HR Specialist" in catturato["prompt"], "un profilo confermato deve arrivare all\'AI"
    finally:
        ai_helpers._chiama_api = originale


def test_fra_omonimi_sceglie_quello_del_settore():
    from services import dossier_albo
    with _con_apify([
        {"firstName": "Mario", "lastName": "Rossi", "headline": "HR Specialist",
         "linkedinUrl": "https://linkedin.com/in/mario-hr"},
        {"firstName": "Mario", "lastName": "Rossi",
         "headline": "Private Banker presso BNL BNP Paribas",
         "linkedinUrl": "https://linkedin.com/in/mario-banker"},
    ]):
        p, nota, altri = dossier_albo._cerca_linkedin("Mario", "Rossi", "BNL BNP Paribas", "Roma")
    assert p["linkedin"].endswith("mario-banker"), p["linkedin"]
    assert not nota, f"con un segnale chiaro non serve avvisare: {nota}"
    assert altri, "gli altri omonimi vanno comunque elencati"


def test_avvisa_quando_nessun_omonimo_e_del_settore():
    """Il caso pericoloso: nome giusto, persona sbagliata. Va dichiarato."""
    from services import dossier_albo
    with _con_apify([
        {"firstName": "Mario", "lastName": "Rossi", "headline": "HR Specialist",
         "linkedinUrl": "https://linkedin.com/in/mario-hr"},
        {"firstName": "Mario", "lastName": "Rossi", "headline": "Purchasing Director",
         "linkedinUrl": "https://linkedin.com/in/mario-acquisti"},
    ]):
        p, nota, _ = dossier_albo._cerca_linkedin("Mario", "Rossi", "BNL BNP Paribas", "Roma")
    assert p is not None
    assert "verificare a mano" in nota, nota


def test_consulente_di_un_altro_mestiere_non_vale_come_conferma():
    """«Consulente informatico» non deve passare per consulente finanziario."""
    from services import dossier_albo
    with _con_apify([{"firstName": "Mario", "lastName": "Rossi",
                      "headline": "Consulente informatico e sviluppatore",
                      "linkedinUrl": "https://linkedin.com/in/mario-it"}]):
        _p, nota, _ = dossier_albo._cerca_linkedin("Mario", "Rossi", "BNL BNP Paribas", "Roma")
    assert "verificare a mano" in nota, nota


def test_omonimi_con_gli_stessi_segnali_sono_dichiarati_ambigui():
    """Due profili ugualmente plausibili non si scelgono col caso."""
    from services import dossier_albo
    with _con_apify([
        {"firstName": "Mario", "lastName": "Rossi", "headline": "Private Banker presso Paribas",
         "linkedinUrl": "https://linkedin.com/in/mario-1"},
        {"firstName": "Mario", "lastName": "Rossi", "headline": "Private Banker presso Paribas",
         "linkedinUrl": "https://linkedin.com/in/mario-2"},
    ]):
        _p, nota, altri = dossier_albo._cerca_linkedin("Mario", "Rossi", "BNL BNP Paribas", "Roma")
    assert "stessi segnali" in nota, nota
    assert altri


def test_un_solo_profilo_ma_fuori_settore_viene_segnalato():
    from services import dossier_albo
    with _con_apify([{"firstName": "Mario", "lastName": "Rossi", "headline": "Barista",
                      "linkedinUrl": "https://linkedin.com/in/mario-bar"}]):
        _p, nota, _ = dossier_albo._cerca_linkedin("Mario", "Rossi", "BNL BNP Paribas", "Roma")
    assert "verificare a mano" in nota, nota


def test_dossier_si_chiude_anche_se_linkedin_e_lai_falliscono():
    """
    Il punto della robustezza: se Apify e Anthropic sono giù, il dossier deve
    uscire lo stesso con i dati dell'albo — quelli non dipendono da nessuno.
    """
    from services import dossier_albo

    with _con_apify(errore="Apify non raggiungibile"), _senza_ai():
        d = dossier_albo.costruisci(PROFILO)

    assert d["ok"] is True
    assert d["albo"]["rete"] == "BNL BNP Paribas"
    assert d["linkedin"] is None and d["sintesi"] is None
    assert any("LinkedIn" in n for n in d["note"])
    assert d["propensione"]["disponibile"], "il coefficiente è locale: deve esserci comunque"


def test_risposta_apify_malformata_non_abbatte_il_dossier():
    """
    Un errore restituito è già gestito; qui l'eccezione arriva DAL PARSING di una
    risposta strana. Il dossier deve chiudersi lo stesso con i dati locali.
    """
    import routes.ricerca as R
    from services import dossier_albo

    originale = R.cerca_apify

    def esplode(*a, **k):
        raise ValueError("risposta Apify illeggibile")
    R.cerca_apify = esplode
    try:
        with _senza_ai():
            d = dossier_albo.costruisci(PROFILO)
    finally:
        R.cerca_apify = originale

    assert d["ok"] is True and d["linkedin"] is None
    assert any("LinkedIn" in n for n in d["note"]), d["note"]
    assert d["propensione"]["disponibile"]


def test_dossier_rifiuta_profilo_senza_cognome():
    from services import dossier_albo
    d = dossier_albo.costruisci({"nome": "Mario", "rete": "Azimut"})
    assert d["ok"] is False and "cognome" in d["errore"].lower()


# ── Azioni sulla scheda: analisi AI e invio in pipeline ─────────────────────

COGNOME_PROVA = "Testpipeline"


def _client():
    from app import app
    app.config["TESTING"] = True
    c = app.test_client()
    with c.session_transaction() as sess:
        sess["autenticato"] = True
        sess["username"] = "test"
    return c


def _pulisci_prova():
    from database import get_db
    db = get_db()
    db.execute("DELETE FROM candidati WHERE cognome = ?", (COGNOME_PROVA,))
    db.commit()
    db.close()


def test_testo_per_analisi_dichiara_la_fonte():
    """L'AI deve sapere quale dato è ufficiale e quale viene da LinkedIn."""
    from routes.albo import _testo_per_analisi
    testo = _testo_per_analisi(
        {"nome": "Mario", "cognome": "Rossi", "rete": "Azimut", "comune": "Roma",
         "provincia": "RM", "eta": 44, "n_cambi": 1},
        {"ruolo": "Private Banker", "sommario": "20 anni di esperienza",
         "url": "https://linkedin.com/in/x"})
    assert "dato ufficiale albo OCF" in testo
    assert "Azimut" in testo and "44 anni" in testo and "Private Banker" in testo
    # Senza LinkedIn il testo resta valido
    solo_albo = _testo_per_analisi({"nome": "Mario", "cognome": "Rossi",
                                    "rete": "Azimut", "comune": "Roma"}, None)
    assert "Consulente finanziario" in solo_albo


def test_analisi_e_invio_in_pipeline(monkeypatch=None):
    """Analisi (AI finta) e salvataggio: il candidato entra con punteggio e stato giusti."""
    import ai_helpers
    import routes.albo as A
    from database import get_db

    _pulisci_prova()
    finta = {"punteggio": 8, "analisi_percorso": "Buon profilo",
             "spunti_contatto": ["spunto uno", "spunto due"],
             "messaggio_outreach": "Ciao Mario,"}
    originale = A.__dict__.get("analizza_profilo_linkedin")
    vero_ai = ai_helpers.analizza_profilo_linkedin
    ai_helpers.analizza_profilo_linkedin = lambda *a, **k: finta
    try:
        c = _client()
        profilo = {"nome": "Mario", "cognome": COGNOME_PROVA, "rete": "Azimut",
                   "comune": "Roma", "provincia": "RM", "eta": 44,
                   "propensione": {"indice": 1.7}}
        linkedin = {"url": "https://linkedin.com/in/test-pipeline-xyz",
                    "ruolo": "Private Banker"}

        r = c.post("/albo/analizza", json={"profilo": profilo, "linkedin": linkedin,
                                           "tipo_profilo": "B"})
        assert r.status_code == 200, r.get_data(as_text=True)
        assert r.get_json()["analisi"]["punteggio"] == 8

        r = c.post("/albo/in-pipeline", json={"profilo": profilo, "linkedin": linkedin,
                                              "analisi": finta, "tipo_profilo": "B"})
        assert r.status_code == 200, r.get_data(as_text=True)
        assert r.get_json()["con_analisi"] is True

        db = get_db()
        riga = db.execute("SELECT * FROM candidati WHERE cognome = ?", (COGNOME_PROVA,)).fetchone()
        db.close()
        assert riga["punteggio"] == 8
        assert riga["stato"] == "Da contattare"
        assert riga["source"] == "ocf"
        assert riga["azienda"] == "Azimut"
        assert "propensione 1.7x" in (riga["note"] or "")
        assert "spunto uno" in (riga["spunti"] or "")

        # Secondo invio: deve essere riconosciuto come duplicato
        r = c.post("/albo/in-pipeline", json={"profilo": profilo, "linkedin": linkedin,
                                              "analisi": finta, "tipo_profilo": "B"})
        assert r.status_code == 409, r.status_code
    finally:
        ai_helpers.analizza_profilo_linkedin = vero_ai
        if originale is not None:
            A.analizza_profilo_linkedin = originale
        _pulisci_prova()


def test_invio_in_pipeline_senza_analisi():
    """Senza analisi il candidato entra lo stesso, come «Da valutare»."""
    from database import get_db

    _pulisci_prova()
    try:
        c = _client()
        r = c.post("/albo/in-pipeline", json={
            "profilo": {"nome": "Anna", "cognome": COGNOME_PROVA, "rete": "Azimut",
                        "comune": "Roma", "provincia": "RM"},
            "linkedin": None, "analisi": None, "tipo_profilo": "A"})
        assert r.status_code == 200, r.get_data(as_text=True)
        assert r.get_json()["con_analisi"] is False

        db = get_db()
        riga = db.execute("SELECT stato, punteggio, gestore FROM candidati WHERE cognome = ?",
                          (COGNOME_PROVA,)).fetchone()
        db.close()
        assert riga["stato"] == "Da valutare"
        assert riga["punteggio"] is None
        assert riga["gestore"] == "Salvatore Sabia"
    finally:
        _pulisci_prova()


if __name__ == "__main__":
    import types
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))
    import logging
    logging.getLogger("database").setLevel(logging.ERROR)

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
