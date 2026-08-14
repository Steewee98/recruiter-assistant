"""
Eval/guardrail per il modulo di triangolazione e per il package connettori.

NON chiama l'API Anthropic (l'AI è mockata) → nessun costo, nessuna dipendenza esterna.
Verifica i layer Axiom/MIT: contratto uniforme, robustezza a cascata, validazione output.

Esecuzione:
    venv/bin/python -m pytest tests/test_triangolazione.py -q
    # oppure senza pytest:
    venv/bin/python tests/test_triangolazione.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def test_dispatcher_e_contratto():
    """Ogni connettore registrato espone FONTE ed è raggiungibile dal dispatcher."""
    import connettori
    assert set(connettori.fonti_registrate()) >= {"linkedin", "ocf", "riconoscimenti"}
    for fonte in connettori.fonti_registrate():
        mod = connettori.get_connettore(fonte)
        assert getattr(mod, "FONTE", None) == fonte


def test_dedup_cross_fonte():
    """La stessa persona da LinkedIn e da OCF (senza URL) deve dare la stessa chiave."""
    from connettori.base import chiave_dedup, profilo_normalizzato
    li = profilo_normalizzato(nome="Mario", cognome="Rossi", azienda="Fideuram", source="linkedin")
    ocf = profilo_normalizzato(nome="Mario", cognome="Rossi", azienda="Fideuram", source="ocf")
    assert chiave_dedup(li) == chiave_dedup(ocf)
    # Con URL LinkedIn, la chiave è basata sull'URL normalizzato
    con_url = profilo_normalizzato(nome="Mario", cognome="Rossi",
                                   url_fonte="https://www.linkedin.com/in/mrossi/", source="linkedin")
    assert chiave_dedup(con_url).startswith("url:linkedin.com/in/mrossi")


def test_lookup_degradano_con_grazia():
    """OCF e riconoscimenti, se disattivati o senza cognome, NON sollevano mai."""
    os.environ.pop("OCF_LOOKUP_ATTIVO", None)
    os.environ.pop("RICONOSCIMENTI_LOOKUP_ATTIVO", None)
    from connettori.ocf import cerca_per_nome as ocf
    from connettori.riconoscimenti import cerca_per_nome as ric
    r1 = ocf("Mario", "Rossi")
    r2 = ric("Mario", "Rossi")
    r3 = ocf("Mario", "")           # cognome mancante
    for r in (r1, r2, r3):
        assert r.ok is False        # disattivati o input incompleto
        assert isinstance(r.errore, str) and r.errore     # errore diagnostico presente
        assert r.to_dict()["fonte"] in ("ocf", "riconoscimenti")


def test_triangolazione_valida_output(monkeypatch):
    """La triangolazione produce un dossier con le chiavi obbligatorie, anche se l'AI
    restituisce un JSON parziale (guardrail di validazione)."""
    import services.triangolazione as tri

    # Mock dell'AI: ritorna un dossier VOLUTAMENTE incompleto
    def fake_ai(candidato, ocf, riconoscimenti):
        return {"preferenza_vs_linkedin": 8, "sintesi_triangolazione": "ok"}
    monkeypatch.setattr(tri, "triangola_profilo", fake_ai)

    esito = tri.triangola({"nome": "Mario", "cognome": "Rossi",
                           "ruolo_attuale": "Private Banker", "azienda": "Fideuram"})
    assert esito["ok"] is True
    d = esito["dossier"]
    for chiave in tri.CHIAVI_DOSSIER:
        assert chiave in d, f"chiave mancante nel dossier: {chiave}"
    # come_contattarlo deve avere i default prudenti
    assert d["come_contattarlo"]["canale_consigliato"]
    assert d["priorita"] == "media"   # default riempito dal validatore
    # Le fonti (anche se fallite) sono riportate per trasparenza
    assert "ocf" in esito["fonti"] and "riconoscimenti" in esito["fonti"]


def test_triangolazione_senza_cognome():
    """Guardrail d'ingresso: senza cognome non si triangola."""
    import services.triangolazione as tri
    esito = tri.triangola({"nome": "Mario", "cognome": ""})
    assert esito["ok"] is False
    assert "cognome" in esito["errore"].lower()


def test_interpreta_query_vuota_non_chiama_api():
    """Query vuota → fallback immediato senza toccare l'API."""
    from ai_helpers import interpreta_query_ricerca
    d = interpreta_query_ricerca("")
    assert set(d.keys()) == {"ruolo", "citta", "azienda", "parole_chiave"}
    assert d["ruolo"] == ""


def test_triangola_da_profilo(monkeypatch):
    """La ricerca smart adatta un profilo grezzo e produce un dossier valido (AI mockata)."""
    import services.triangolazione as tri
    from connettori.base import RisultatoFonte
    monkeypatch.setattr(tri, "triangola_profilo",
                        lambda c, o, r: {"preferenza_vs_linkedin": 6, "priorita": "alta"})
    # Mocka i lookup esterni per non fare chiamate HTTP reali durante il test
    monkeypatch.setattr(tri, "ocf_lookup", lambda n, c, contesto=None, forza=False: RisultatoFonte(fonte="ocf"))
    monkeypatch.setattr(tri, "ric_lookup", lambda n, c, contesto=None, forza=False: RisultatoFonte(fonte="riconoscimenti"))
    profilo = {"nome": "Anna", "cognome": "Bianchi", "ruolo": "Private Banker",
               "azienda": "Fideuram", "sommario": "20 anni nel wealth"}
    esito = tri.triangola_da_profilo(profilo, usa_esterni=True)
    assert esito["ok"] is True
    assert esito["dossier"]["come_contattarlo"]["canale_consigliato"]
    # usa_esterni=True forza i lookup: le fonti sono presenti nell'output
    assert "ocf" in esito["fonti"] and "riconoscimenti" in esito["fonti"]


# ── Runner minimale senza pytest ─────────────────────────────────────────────
if __name__ == "__main__":
    import types

    class _MP:
        """monkeypatch fatto in casa per il runner senza pytest."""
        def __init__(self): self._undo = []
        def setattr(self, obj, name, val):
            self._undo.append((obj, name, getattr(obj, name)))
            setattr(obj, name, val)
        def undo(self):
            for obj, name, val in reversed(self._undo):
                setattr(obj, name, val)

    passati, falliti = 0, 0
    for nome, fn in sorted(globals().items()):
        if not (nome.startswith("test_") and isinstance(fn, types.FunctionType)):
            continue
        mp = _MP()
        try:
            fn(mp) if "monkeypatch" in fn.__code__.co_varnames else fn()
            print(f"  ✓ {nome}")
            passati += 1
        except Exception as e:
            print(f"  ✗ {nome}: {type(e).__name__}: {e}")
            falliti += 1
        finally:
            mp.undo()
    print(f"\n{passati} passati, {falliti} falliti")
    sys.exit(1 if falliti else 0)
