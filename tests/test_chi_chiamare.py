"""
Guardrail della classifica «chi chiamare adesso».

Gira sul database reale in sola lettura per la parte di albo, e ripulisce
quello che scrive. Nessuna spesa: niente Apify, niente AI.

Esecuzione:
    venv/bin/python tests/test_chi_chiamare.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def test_classifica_ordinata_e_coerente():
    """La lista deve essere ordinata per punteggio decrescente, senza buchi."""
    from services import chi_chiamare as C

    esito = C.genera(quanti=40)
    assert esito["ok"], esito
    assert esito["quanti"] > 0, "il bacino di Roma non può essere vuoto"

    d = C.leggi(40)
    righe = d["righe"]
    assert len(righe) == esito["quanti"]

    posizioni = [r["posizione"] for r in righe]
    assert posizioni == sorted(posizioni), "le posizioni devono essere in ordine"
    assert posizioni[0] == 1, "la classifica parte da 1"

    indici = [float(r["indice"]) for r in righe]
    assert indici == sorted(indici, reverse=True), \
        "il punteggio deve scendere lungo la lista, altrimenti l'ordine non vuol dire niente"


def test_ogni_riga_sa_spiegare_il_proprio_punteggio():
    """
    Un punteggio che chi telefona non sa spiegare non viene usato: ogni riga
    deve portare il motivo in chiaro.
    """
    from services import chi_chiamare as C

    for r in C.leggi(20)["righe"]:
        assert r["perche"], f"{r['cognome']} non ha una spiegazione"
        assert any("%" in p for p in r["perche"]), \
            "la spiegazione deve contenere il dato osservato, non solo un aggettivo"
        assert r["indice"] and float(r["indice"]) > 0
        assert r["rete"], "senza rete il punteggio non ha fondamento"


def test_esclude_chi_e_gia_stato_lavorato():
    """Chi è già in dossier o in pipeline non deve ricomparire fra i da chiamare."""
    from database import get_db
    from services import chi_chiamare as C

    chiavi = {r["chiave"] for r in C.leggi(300)["righe"]}
    if not chiavi:
        return

    db = get_db()
    try:
        segnaposto = ",".join(["?"] * len(chiavi))
        gia = db.execute(
            f"SELECT chiave FROM ocf_dossier WHERE chiave IN ({segnaposto})",
            list(chiavi)).fetchall()
    finally:
        db.close()
    assert not gia, f"{len(gia)} nominativi già lavorati sono rientrati in lista"


def test_solo_provincia_operativa_e_sopra_eta_minima():
    from services import albo_ocf, chi_chiamare as C

    for r in C.leggi(100)["righe"]:
        assert r["provincia"] == albo_ocf.PROVINCIA_OPERATIVA, r
        assert r["rete"] != albo_ocf.RETE_PROPRIA, "i nostri non vanno chiamati"
        assert r["eta"] is None or r["eta"] >= C.ETA_MINIMA, r


def test_reti_estinte_fuori_dai_tassi():
    """
    Una rete senza più iscritti è stata assorbita: il suo «100% è uscito» è una
    fusione, non mobilità. Se rientra, la cima della classifica delle reti
    diventa un elenco di banche che non esistono più.
    """
    from database import get_db
    from services import propensione as P

    par = P.parametri(ricalcola=True)
    assert par["pronto"]

    db = get_db()
    try:
        attivi = {r["rete"]: r["n"] for r in db.execute(
            "SELECT rete, COUNT(*) AS n FROM ocf_iscritti WHERE attivo = TRUE GROUP BY rete"
        ).fetchall()}
    finally:
        db.close()

    for rete, info in par["reti"].items():
        assert attivi.get(rete, 0) > 0, f"«{rete}» non ha più iscritti: è una rete estinta"
        assert info["mossi"] < info["osservati"], \
            f"«{rete}»: usciti tutti quanti, è un assorbimento travestito da mobilità"


def test_la_lista_resta_ferma_fra_una_generazione_e_laltra():
    """
    Chi telefona lavora la lista per giorni: leggerla due volte senza
    rigenerarla deve dare esattamente lo stesso ordine.
    """
    from services import chi_chiamare as C

    a = [(r["posizione"], r["chiave"]) for r in C.leggi(30)["righe"]]
    b = [(r["posizione"], r["chiave"]) for r in C.leggi(30)["righe"]]
    assert a == b and a, "la classifica salvata non è stabile fra due letture"


if __name__ == "__main__":
    import logging
    import types

    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))
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
