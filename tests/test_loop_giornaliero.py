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

    - Anna  → LinkedIn trovato, punteggio 9  → deve entrare in pipeline
    - Bruno → nessun profilo LinkedIn         → deve essere scartato prima dell'AI
    - Carla → LinkedIn trovato, punteggio 4   → deve essere scartata dopo l'AI
    """

    def __init__(self, budget=None):
        self.budget = budget or {"noto": True, "usato": 1.0, "tetto": 29.0, "residuo": 28.0}
        self.analisi_fatte = []

    def __enter__(self):
        from services import albo_ocf, dossier_albo, loop_giornaliero
        import ai_helpers

        self._orig = {
            "cerca": albo_ocf.cerca,
            "gruppo": dossier_albo.cerca_linkedin_gruppo,
            "ai": ai_helpers.analizza_profilo_linkedin,
            "budget": loop_giornaliero.budget_apify,
        }
        persone = _persone()
        albo_ocf.cerca = lambda **k: {"totale": len(persone), "profili": persone[:k.get("limite", 10)],
                                      "esclusi_lavorati": 0}

        def gruppo(gente, **k):
            fuori = {}
            for p in gente:
                if p["cognome"] == "Loopdue":
                    fuori[p["chiave"]] = {"profilo": None, "omonimi": [],
                                          "nota": "Nessun profilo LinkedIn corrispondente."}
                else:
                    fuori[p["chiave"]] = {"profilo": {
                        "nome": p["nome"], "cognome": p["cognome"],
                        "ruolo": "Consulente finanziario presso Azimut",
                        "azienda": "Azimut", "sommario": "esperienza",
                        "linkedin": f"https://linkedin.com/in/{p['chiave']}"},
                        "nota": "", "omonimi": []}
            return fuori
        dossier_albo.cerca_linkedin_gruppo = gruppo

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
        albo_ocf.cerca = self._orig["cerca"]
        dossier_albo.cerca_linkedin_gruppo = self._orig["gruppo"]
        ai_helpers.analizza_profilo_linkedin = self._orig["ai"]
        loop_giornaliero.budget_apify = self._orig["budget"]
        return False


def _pulisci():
    from database import get_db
    db = get_db()
    for c in COGNOMI_PROVA:
        db.execute("DELETE FROM candidati WHERE cognome = ?", (c,))
    db.execute("DELETE FROM ocf_dossier WHERE chiave IN ('loop_k1','loop_k2','loop_k3')")
    # Le righe di storico create dai test vanno via: `esegui()` ne scrive una a
    # ogni giro, e i nomi finti sono l'unico modo per riconoscerle senza toccare
    # lo storico vero.
    db.execute("DELETE FROM ocf_loop_run WHERE dettaglio LIKE '%Loop%'")
    db.execute("""DELETE FROM ocf_loop_run
                   WHERE stato = 'saltata_budget'
                     AND nota LIKE '%28.5%'""")
    db.commit()
    db.close()


def test_giro_completo_importa_solo_chi_supera_la_soglia():
    from database import get_db
    from services import loop_giornaliero as L

    _pulisci()
    try:
        with _finto_mondo() as mondo:
            r = L.esegui(limite=3)

        assert r["ok"], r
        assert r["esaminati"] == 3
        assert r["senza_linkedin"] == 1, "Bruno non ha LinkedIn: va scartato"
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
        _pulisci()


def test_ogni_persona_toccata_viene_registrata():
    """Anche gli scarti: senza traccia domani si ripagherebbe la stessa ricerca."""
    from database import get_db
    from services import loop_giornaliero as L

    _pulisci()
    try:
        with _finto_mondo():
            L.esegui(limite=3)
        db = get_db()
        righe = {r["chiave"]: r["esito"] for r in db.execute(
            "SELECT chiave, esito FROM ocf_dossier WHERE chiave LIKE 'loop_k%'").fetchall()}
        db.close()
        assert righe.get("loop_k1") == "in_pipeline"
        assert righe.get("loop_k2") == "senza_linkedin"
        assert righe.get("loop_k3") == "scartato_punteggio"
    finally:
        _pulisci()


def test_si_ferma_se_il_budget_e_finito():
    """Il freno che evita di consumare i soldi delle ricerche a mano."""
    from services import loop_giornaliero as L

    _pulisci()
    try:
        with _finto_mondo(budget={"noto": True, "usato": 28.5, "tetto": 29.0,
                                  "residuo": 0.5, "fine_ciclo": "2026-09-18"}) as mondo:
            r = L.esegui(limite=3)
        assert not r["ok"]
        assert r["esaminati"] == 0, "non deve nemmeno cercare"
        assert "budget" in r["nota"].lower()
        assert not mondo.analisi_fatte, "nessuna chiamata AI a budget esaurito"
    finally:
        _pulisci()


def test_budget_non_leggibile_non_blocca():
    """Se Apify non risponde sul budget non ci si ferma: si prova, con cautela."""
    from services import loop_giornaliero as L

    _pulisci()
    try:
        with _finto_mondo(budget={"noto": False, "motivo": "rete giù"}):
            r = L.esegui(limite=3)
        assert r["ok"] and r["esaminati"] == 3
    finally:
        _pulisci()


def test_storico_registrato():
    from database import get_db
    from services import loop_giornaliero as L

    _pulisci()
    try:
        with _finto_mondo():
            L.esegui(limite=3)
        ultime = L.ultime_esecuzioni(1)
        assert ultime and ultime[0]["stato"] == "completata"
        assert ultime[0]["importati"] == 1 and ultime[0]["senza_linkedin"] == 1
        assert "Loopuno" in (ultime[0]["dettaglio"] or ""), "il dettaglio deve dire chi e perché"
        db = get_db()
        db.execute("DELETE FROM ocf_loop_run WHERE id = ?", (ultime[0]["id"],))
        db.commit()
        db.close()
    finally:
        _pulisci()


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
