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


def _con_apify(items):
    """Sostituisce la ricerca Apify dentro routes.ricerca (importata a runtime)."""
    import routes.ricerca as R
    R.cerca_apify = _finto_apify(items)


def test_scarta_chi_non_ha_il_nome_giusto():
    from services import dossier_albo
    _con_apify([{"firstName": "Luca", "lastName": "Bianchi", "headline": "Private Banker",
                 "linkedinUrl": "https://linkedin.com/in/luca"}])
    p, nota, altri = dossier_albo._cerca_linkedin("Mario", "Rossi", "BNL BNP Paribas", "Roma")
    assert p is None, "un omonimo sbagliato non deve finire nel dossier"
    assert "Nessuna corrispondenza" in nota and "Luca Bianchi" in nota


def test_fra_omonimi_sceglie_quello_del_settore():
    from services import dossier_albo
    _con_apify([
        {"firstName": "Mario", "lastName": "Rossi", "headline": "HR Specialist",
         "linkedinUrl": "https://linkedin.com/in/mario-hr"},
        {"firstName": "Mario", "lastName": "Rossi",
         "headline": "Private Banker presso BNL BNP Paribas",
         "linkedinUrl": "https://linkedin.com/in/mario-banker"},
    ])
    p, nota, altri = dossier_albo._cerca_linkedin("Mario", "Rossi", "BNL BNP Paribas", "Roma")
    assert p["linkedin"].endswith("mario-banker"), p["linkedin"]
    assert not nota, f"con un segnale chiaro non serve avvisare: {nota}"
    assert altri, "gli altri omonimi vanno comunque elencati"


def test_avvisa_quando_nessun_omonimo_e_del_settore():
    """Il caso pericoloso: nome giusto, persona sbagliata. Va dichiarato."""
    from services import dossier_albo
    _con_apify([
        {"firstName": "Mario", "lastName": "Rossi", "headline": "HR Specialist",
         "linkedinUrl": "https://linkedin.com/in/mario-hr"},
        {"firstName": "Mario", "lastName": "Rossi", "headline": "Purchasing Director",
         "linkedinUrl": "https://linkedin.com/in/mario-acquisti"},
    ])
    p, nota, _ = dossier_albo._cerca_linkedin("Mario", "Rossi", "BNL BNP Paribas", "Roma")
    assert p is not None
    assert "verificare a mano" in nota, nota


def test_un_solo_profilo_ma_fuori_settore_viene_segnalato():
    from services import dossier_albo
    _con_apify([{"firstName": "Mario", "lastName": "Rossi", "headline": "Barista",
                 "linkedinUrl": "https://linkedin.com/in/mario-bar"}])
    _p, nota, _ = dossier_albo._cerca_linkedin("Mario", "Rossi", "BNL BNP Paribas", "Roma")
    assert "verificare a mano" in nota, nota


def test_dossier_si_chiude_anche_se_linkedin_e_lai_falliscono():
    """
    Il punto della robustezza: se Apify e Anthropic sono giù, il dossier deve
    uscire lo stesso con i dati dell'albo — quelli non dipendono da nessuno.
    """
    import routes.ricerca as R
    from services import dossier_albo

    def apify_rotto(*a, **k):
        return None, "Apify non raggiungibile"
    R.cerca_apify = apify_rotto

    import ai_helpers
    originale = ai_helpers._chiama_api
    ai_helpers._chiama_api = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("credito esaurito"))
    try:
        d = dossier_albo.costruisci(PROFILO)
    finally:
        ai_helpers._chiama_api = originale

    assert d["ok"] is True
    assert d["albo"]["rete"] == "BNL BNP Paribas"
    assert d["linkedin"] is None and d["sintesi"] is None
    assert any("LinkedIn" in n for n in d["note"])
    assert d["propensione"]["disponibile"], "il coefficiente è locale: deve esserci comunque"


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
