"""
Eval/guardrail per l'albo OCF: parsing dell'elenco ufficiale e radar movimenti.

I test di parsing sono offline (ZIP costruito in memoria): nessuna rete, nessun costo.
I test del diff girano su un elenco isolato ('test_albo'), che viene ripulito
alla fine — non toccano lo snapshot reale 'abilitati'.

Esecuzione:
    venv/bin/python tests/test_albo_ocf.py
"""

import io
import os
import sys
import zipfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ELENCO_TEST = "test_albo"


def _zip_finto(righe, data_testo="03 settembre 2026"):
    """Costruisce in memoria un archivio nel formato pubblicato da OCF."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("_disclaimer.txt", f"Nei presenti elenchi aggiornati al {data_testo} sono riportate…")
        z.writestr("_HEADER_CF_ABILITATI.txt",
                   "NOME,COGNOME,DATA_NASCITA,LUOGO_NASCITA,SIGLA_PROVINCIA_NASCITA,"
                   "INDIRIZZO,CIVICO,CAP,COMUNE,PROVINCIA,DENOMINAZIONE_SOCIETA_CONSULENZA,REGIONE")
        csv = "\n".join(
            '"{}","{}","{}","ROMA","RM","VIA TEST","1","00100","{}","{}","{}","LAZIO"'.format(*r)
            for r in righe)
        z.writestr("LAZIO_CFAB.csv", csv)
    return buf.getvalue()


# ── Parsing e minimizzazione ─────────────────────────────────────────────────

def test_parsing_campi_essenziali():
    from connettori.ocf_elenco import leggi_iscritti, data_elenco
    z = _zip_finto([("MARIO", "ROSSI", "18/01/1977", "ROMA", "RM", "FINECOBANK BANCA FINECO S.P.A.; ")])
    righe = list(leggi_iscritti(z))
    assert len(righe) == 1
    r = righe[0]
    assert r["nome"] == "Mario" and r["cognome"] == "Rossi"
    assert r["anno_nascita"] == 1977
    assert r["comune"] == "Roma" and r["provincia"] == "RM"
    assert r["rete"] == "FinecoBank", r["rete"]
    assert str(data_elenco(z)) == "2026-09-03"


def test_minimizzazione_dati_personali():
    """Indirizzo, CAP, data di nascita esatta e luogo di nascita NON escono dal parser."""
    from connettori.ocf_elenco import leggi_iscritti
    z = _zip_finto([("MARIO", "ROSSI", "18/01/1977", "ROMA", "RM", "AZIMUT CAPITAL MANAGEMENT SGR SPA")])
    r = list(leggi_iscritti(z))[0]
    vietati = {"indirizzo", "civico", "cap", "data_nascita", "luogo_nascita"}
    assert not (set(r) & vietati), f"campi sensibili trapelati: {set(r) & vietati}"
    testo = str(r).lower()
    assert "18/01/1977" not in testo and "via test" not in testo and "00100" not in testo


def test_chiave_stabile_e_irreversibile():
    from connettori.ocf_elenco import chiave_persona
    a = chiave_persona("Mario", "Rossi", "18/01/1977")
    b = chiave_persona("  mario ", "ROSSI", "18/01/1977")
    c = chiave_persona("Mario", "Rossi", "19/01/1977")
    assert a == b, "la chiave deve ignorare maiuscole e spazi"
    assert a != c, "due date diverse devono dare chiavi diverse"
    assert "1977" not in a and "Rossi" not in a


def test_normalizza_rete():
    from connettori.ocf_elenco import normalizza_rete
    assert normalizza_rete("FIDEURAM - INTESA SANPAOLO PRIVATE BANKING SPA IN FORMA ABBREVIATA") == "Fideuram"
    assert normalizza_rete("BANCA MEDIOLANUM SPA") == "Banca Mediolanum"
    assert normalizza_rete("FINECOBANK BANCA FINECO S.P.A.; ") == "FinecoBank"
    assert normalizza_rete("") == ""
    # Intesa Sanpaolo Private Banking non deve collassare su "Intesa Sanpaolo"
    assert normalizza_rete("INTESA SANPAOLO PRIVATE BANKING SPA") == "Intesa Sanpaolo Private Banking"


def test_righe_malformate_non_fermano_il_parsing():
    """Un CSV storto in mezzo a 56.000 righe non deve far saltare la sincronizzazione."""
    from connettori.ocf_elenco import leggi_iscritti
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("LAZIO_CFAB.csv",
                   'riga,corta,rotta\n'
                   '"MARIO","ROSSI","18/01/1977","ROMA","RM","VIA X","1","00100","ROMA","RM","AZIMUT CAPITAL MANAGEMENT SGR SPA","LAZIO"\n'
                   '"","","","","","","","","","","",""\n')
    righe = list(leggi_iscritti(buf.getvalue()))
    assert len(righe) == 1 and righe[0]["cognome"] == "Rossi"


# ── Radar movimenti (diff fra due elenchi) ───────────────────────────────────

def _pulisci_test(db):
    chiavi = [r["chiave"] for r in db.execute(
        "SELECT chiave FROM ocf_iscritti WHERE elenco = ?", (ELENCO_TEST,)).fetchall()]
    for c in chiavi:
        db.execute("DELETE FROM ocf_movimenti WHERE chiave = ?", (c,))
    db.execute("DELETE FROM ocf_iscritti WHERE elenco = ?", (ELENCO_TEST,))
    db.execute("DELETE FROM ocf_sync WHERE elenco = ?", (ELENCO_TEST,))
    db.commit()


def test_diff_rileva_cambio_rete_nuovo_e_uscito():
    from database import get_db
    from services import albo_ocf

    db = get_db()
    _pulisci_test(db)

    # Primo elenco: tre consulenti
    z1 = _zip_finto([
        ("ANNA",  "TESTUNO",  "01/01/1980", "ROMA", "RM", "BANCA MEDIOLANUM SPA"),
        ("BRUNO", "TESTDUE",  "02/02/1975", "ROMA", "RM", "FINECOBANK BANCA FINECO S.P.A."),
        ("CARLA", "TESTTRE",  "03/03/1990", "ROMA", "RM", "AZIMUT CAPITAL MANAGEMENT SGR SPA"),
    ])
    r1 = albo_ocf.sincronizza(ELENCO_TEST, zip_bytes=z1)
    assert r1["ok"] and r1["totale"] == 3
    assert r1["nuovi"] == 0 and r1["cambi_rete"] == 0, \
        "la PRIMA sincronizzazione non deve inventare movimenti"

    # Secondo elenco: Anna cambia rete, Bruno resta, Carla sparisce, Dario entra
    z2 = _zip_finto([
        ("ANNA",  "TESTUNO",  "01/01/1980", "ROMA", "RM", "BANCA GENERALI SPA"),
        ("BRUNO", "TESTDUE",  "02/02/1975", "ROMA", "RM", "FINECOBANK BANCA FINECO S.P.A."),
        ("DARIO", "TESTQUATTRO", "04/04/1985", "ROMA", "RM", "AZIMUT CAPITAL MANAGEMENT SGR SPA"),
    ], data_testo="10 ottobre 2026")
    r2 = albo_ocf.sincronizza(ELENCO_TEST, zip_bytes=z2)
    assert r2["ok"], r2
    assert r2["cambi_rete"] == 1, f"atteso 1 cambio rete, ottenuto {r2['cambi_rete']}"
    assert r2["nuovi"] == 1, f"atteso 1 nuovo, ottenuto {r2['nuovi']}"
    assert r2["usciti"] == 1, f"atteso 1 uscito, ottenuto {r2['usciti']}"

    # Il movimento registrato dice da dove a dove
    mov = [m for m in albo_ocf.movimenti(tipo="cambio_rete", giorni=1)
           if m["cognome"] == "Testuno"]
    assert mov and mov[0]["rete_precedente"] == "Banca Mediolanum"
    assert mov[0]["rete_nuova"] == "Banca Generali"

    # Anzianità: dopo un cambio osservato non è più una stima
    riga = db.execute("SELECT rete, rete_dal_stimata, n_cambi FROM ocf_iscritti "
                      "WHERE elenco = ? AND cognome = 'Testuno'", (ELENCO_TEST,)).fetchone()
    assert riga["rete"] == "Banca Generali"
    assert riga["rete_dal_stimata"] is False, "dopo un cambio osservato l'anzianità è certa"
    assert riga["n_cambi"] == 1

    # Chi è uscito resta in archivio ma non attivo
    fuori = db.execute("SELECT attivo FROM ocf_iscritti WHERE elenco = ? AND cognome = 'Testtre'",
                       (ELENCO_TEST,)).fetchone()
    assert fuori["attivo"] is False

    _pulisci_test(db)
    db.close()


def test_sincronizzazione_idempotente():
    """Rieseguire la stessa sincronizzazione non deve generare movimenti falsi."""
    from database import get_db
    from services import albo_ocf
    db = get_db()
    _pulisci_test(db)
    z = _zip_finto([("ELIO", "TESTCINQUE", "05/05/1970", "ROMA", "RM", "BANCA GENERALI SPA")])
    albo_ocf.sincronizza(ELENCO_TEST, zip_bytes=z)
    r = albo_ocf.sincronizza(ELENCO_TEST, zip_bytes=z)
    assert r["cambi_rete"] == 0 and r["nuovi"] == 0 and r["usciti"] == 0, r
    _pulisci_test(db)
    db.close()


def test_elenco_vuoto_non_cancella_lo_snapshot():
    """Guardrail: un download andato male non deve svuotare l'albo."""
    from database import get_db
    from services import albo_ocf
    db = get_db()
    _pulisci_test(db)
    albo_ocf.sincronizza(ELENCO_TEST, zip_bytes=_zip_finto(
        [("FRANCA", "TESTSEI", "06/06/1965", "ROMA", "RM", "AZIMUT CAPITAL MANAGEMENT SGR SPA")]))
    r = albo_ocf.sincronizza(ELENCO_TEST, zip_bytes=_zip_finto([]))
    assert not r["ok"] and "vuoto" in (r["errore"] or "").lower()
    n = db.execute("SELECT COUNT(*) AS n FROM ocf_iscritti WHERE elenco = ? AND attivo = TRUE",
                   (ELENCO_TEST,)).fetchone()["n"]
    assert n == 1, "lo snapshot precedente deve restare intatto"
    _pulisci_test(db)
    db.close()


def test_verifica_dichiara_omonimia():
    from database import get_db
    from services import albo_ocf
    db = get_db()
    _pulisci_test(db)
    albo_ocf.sincronizza(ELENCO_TEST, zip_bytes=_zip_finto([
        ("GINO", "TESTSETTE", "07/07/1980", "ROMA", "RM", "BANCA GENERALI SPA"),
        ("GINO", "TESTSETTE", "08/08/1988", "ROMA", "RM", "AZIMUT CAPITAL MANAGEMENT SGR SPA"),
    ]))
    v = albo_ocf.verifica("Gino", "Testsette")
    assert v["trovato"] and v["ambiguo"] and len(v["iscritti"]) == 2
    assert albo_ocf.verifica("", "")["trovato"] is False
    _pulisci_test(db)
    db.close()


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
