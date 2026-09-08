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
