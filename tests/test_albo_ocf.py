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


# ── Eventi societari: fusioni e rinomine non sono passaggi ───────────────────

def test_stesso_gruppo():
    from connettori.ocf_elenco import stesso_gruppo
    assert stesso_gruppo("Sanpaolo Invest", "Fideuram"), "giro interno al gruppo Intesa"
    assert stesso_gruppo("Credito Emiliano", "Credem Euromobiliare")
    assert stesso_gruppo("Chebanca!", "Mediobanca Premier")
    assert not stesso_gruppo("Azimut", "Fideuram"), "questo è un passaggio vero"
    assert not stesso_gruppo("", "Fideuram")


def test_marcatore_N_negli_elenchi_vecchi():
    """Negli elenchi 2022 il valore mancante è il letterale \\N, non la stringa vuota."""
    from connettori.ocf_elenco import normalizza_rete
    assert normalizza_rete("\\N") == ""
    assert normalizza_rete("NULL") == ""


def test_confronto_marca_flussi_di_massa_e_di_gruppo():
    """
    Una rete che cambia nome (tutti da A a B) e un giro interno al gruppo devono
    risultare 'societari'; il passaggio singolo verso un concorrente no.
    """
    from services.albo_ocf import _confronta
    prima, dopo = {}, {}
    for i in range(40):                       # rinomina di massa: Deutsche → Zurich
        k = f"massa{i}"
        prima[k] = {"rete": "Deutsche Bank"}
        dopo[k] = {"rete": "Zurich Bank", "nome": "A", "cognome": f"B{i}",
                   "comune": "Roma", "provincia": "RM"}
    prima["gruppo1"] = {"rete": "Sanpaolo Invest"}      # giro interno al gruppo
    dopo["gruppo1"] = {"rete": "Fideuram", "nome": "C", "cognome": "D",
                       "comune": "Roma", "provincia": "RM"}
    prima["vero1"] = {"rete": "Azimut"}                 # passaggio vero
    dopo["vero1"] = {"rete": "Fideuram", "nome": "E", "cognome": "F",
                     "comune": "Milano", "provincia": "MI"}

    import datetime
    mov = _confronta(prima, dopo, datetime.date(2026, 1, 1), "a", "b")
    veri = [m for m in mov if not m["societario"]]
    assert len(mov) == 42
    assert len(veri) == 1 and veri[0]["chiave"] == "vero1", \
        f"atteso 1 passaggio vero, ottenuti {[m['chiave'] for m in veri]}"


def test_zip_troncato_recupera_le_voci_intere():
    """Le copie archiviate sono tagliate a 1 MiB: le voci intere vanno recuperate lo stesso."""
    import io as _io, zipfile as _zip
    from connettori.ocf_storico import csv_da_zip_troncato
    buf = _io.BytesIO()
    with _zip.ZipFile(buf, "w", _zip.ZIP_DEFLATED) as z:
        z.writestr("LAZIO_CFAB.csv", '"A","B","01/01/1980","R","RM","V","1","00100","ROMA","RM","AZIMUT CAPITAL MANAGEMENT SGR SPA","LAZIO"\n' * 50)
        z.writestr("VENETO_CFAB.csv", '"C","D","02/02/1980","R","RM","V","1","00100","ROMA","RM","BANCA GENERALI SPA","VENETO"\n' * 50)
    intero = buf.getvalue()
    tagliato = intero[:int(len(intero) * 0.6)]      # via la coda e l'ultima voce
    recuperati = csv_da_zip_troncato(tagliato)
    assert "LAZIO_CFAB.csv" in recuperati, "la prima voce doveva essere recuperata"
    assert len(recuperati["LAZIO_CFAB.csv"].splitlines()) == 50


def test_squadra_rilevata_e_colleghi_esclusi():
    """
    Tre consulenti della stessa rete e provincia che passano insieme alla stessa
    destinazione = una squadra. Chi è già andato via non deve comparire fra i
    colleghi rimasti.
    """
    from database import get_db
    from services import albo_ocf
    db = get_db()
    _pulisci_test(db)
    prima = [("UNO", "SQUADRAA", "01/01/1980", "ROMA", "RM", "AZIMUT CAPITAL MANAGEMENT SGR SPA"),
             ("DUE", "SQUADRAB", "02/01/1980", "ROMA", "RM", "AZIMUT CAPITAL MANAGEMENT SGR SPA"),
             ("TRE", "SQUADRAC", "03/01/1980", "ROMA", "RM", "AZIMUT CAPITAL MANAGEMENT SGR SPA"),
             ("QUATTRO", "RESTAQUI", "04/01/1980", "ROMA", "RM", "AZIMUT CAPITAL MANAGEMENT SGR SPA")]
    albo_ocf.sincronizza(ELENCO_TEST, zip_bytes=_zip_finto(prima))
    dopo = [(n, c, d, l, pr, "BANCA GENERALI SPA" if c != "RESTAQUI" else s)
            for n, c, d, l, pr, s in prima]
    albo_ocf.sincronizza(ELENCO_TEST, zip_bytes=_zip_finto(dopo, data_testo="10 ottobre 2026"))

    sq = [x for x in albo_ocf.squadre_in_movimento(min_persone=2, giorni=2, limite=50)
          if x["rete_precedente"] == "Azimut" and x["rete_nuova"] == "Banca Generali"
          and x["provincia"] == "RM"]
    assert sq, "la squadra doveva essere rilevata"
    assert sq[0]["persone"] >= 3, sq[0]["persone"]

    rimasti = albo_ocf.colleghi_rimasti("Azimut", "RM")
    cognomi = {p["cognome"] for p in rimasti["profili"]}
    assert "Squadraa" not in cognomi, "chi si è già mosso non è un collega rimasto"

    _pulisci_test(db)
    db.close()


# ── Coefficiente di propensione ──────────────────────────────────────────────

def test_coefficiente_ordina_le_reti_come_i_dati():
    """Una rete da cui si esce molto deve dare un coefficiente più alto di una stabile."""
    from services import propensione
    par = {"pronto": True, "base": 0.07,
           "reti": {"ReteMobile": {"tasso": 0.15, "osservati": 800, "mossi": 120},
                    "ReteStabile": {"tasso": 0.03, "osservati": 900, "mossi": 27}},
           "eta": {}}
    a = propensione.coefficiente("ReteMobile", 45, par)
    b = propensione.coefficiente("ReteStabile", 45, par)
    assert a["indice"] > b["indice"] * 2, (a["indice"], b["indice"])
    assert a["perche"], "il coefficiente deve dire perché"


def test_coefficiente_applica_letà():
    """A parità di rete, gli over 65 devono risultare meno propensi dei giovani."""
    from services import propensione
    par = {"pronto": True, "base": 0.07,
           "reti": {"R": {"tasso": 0.10, "osservati": 500, "mossi": 50}}, "eta": {}}
    giovane = propensione.coefficiente("R", 30, par)["indice"]
    anziano = propensione.coefficiente("R", 70, par)["indice"]
    assert giovane > anziano, (giovane, anziano)
    assert propensione.fascia_eta(None) == "sconosciuta"
    assert propensione.coefficiente("R", None, par)["indice"] > 0


def test_rete_sconosciuta_usa_la_media():
    from services import propensione
    par = {"pronto": True, "base": 0.07, "reti": {}, "eta": {}}
    c = propensione.coefficiente("Banca Mai Vista", 45, par)
    assert c["disponibile"] and abs(c["indice"] - 1.01) < 0.05, c["indice"]
    assert "media di mercato" in " ".join(c["perche"])


def test_coefficiente_degrada_senza_parametri():
    """Senza storico il coefficiente si dichiara non disponibile, non inventa numeri."""
    from services import propensione
    c = propensione.coefficiente("Azimut", 40, {"pronto": False, "motivo": "niente storico"})
    assert c["disponibile"] is False and c["motivo"]


def test_squadre_filtrate_su_una_rete():
    """Il filtro per rete deve tenere sia le squadre in entrata sia quelle in uscita."""
    from services import albo_ocf
    sq = albo_ocf.squadre_in_movimento(min_persone=2, limite=50,
                                       solo_rete=albo_ocf.RETE_PROPRIA)
    for s in sq:
        assert albo_ocf.RETE_PROPRIA in (s["rete_precedente"], s["rete_nuova"]), s
        assert "verso_di_noi" in s and "in_corso" in s


def test_squadre_solo_entrate_e_solo_provincia_operativa():
    """
    Il filtro operativo: solo gruppi ENTRATI in Fideuram e solo sulla piazza di
    Roma. Un gruppo uscito da Fideuram, o entrato ma a Milano, non deve comparire.
    """
    from services import albo_ocf
    sq = albo_ocf.squadre_in_movimento(
        min_persone=2, limite=50, verso=albo_ocf.RETE_PROPRIA,
        solo_provincia=albo_ocf.PROVINCIA_OPERATIVA)
    assert sq, "ci si aspetta almeno un gruppo entrato in Fideuram a Roma"
    for s in sq:
        assert s["rete_nuova"] == albo_ocf.RETE_PROPRIA, s
        assert s["provincia"] == albo_ocf.PROVINCIA_OPERATIVA, s
        assert s["rete_precedente"] != albo_ocf.RETE_PROPRIA


def test_segnalazione_si_archivia():
    """Una volta letta, la segnalazione non deve ripresentarsi."""
    from services import albo_ocf
    albo_ocf.segna_squadre_viste(provincia=albo_ocf.PROVINCIA_OPERATIVA,
                                 verso=albo_ocf.RETE_PROPRIA)
    residue = albo_ocf.squadre_in_movimento(
        min_persone=2, limite=50, verso=albo_ocf.RETE_PROPRIA,
        solo_provincia=albo_ocf.PROVINCIA_OPERATIVA, solo_non_viste=True)
    assert residue == [], f"restano {len(residue)} segnalazioni non archiviate"


def test_pianificatore_spento_in_locale():
    """In sviluppo il pianificatore non deve partire da solo e scaricare 56.000 righe."""
    import os
    from routes import albo
    vecchi = (os.environ.pop("ALBO_SYNC_AUTO", None), os.environ.pop("RAILWAY_ENVIRONMENT", None))
    try:
        assert albo.avvia_pianificatore() is False
        os.environ["ALBO_SYNC_AUTO"] = "1"
        assert albo.avvia_pianificatore() is True
    finally:
        os.environ.pop("ALBO_SYNC_AUTO", None)
        for chiave, valore in zip(("ALBO_SYNC_AUTO", "RAILWAY_ENVIRONMENT"), vecchi):
            if valore is not None:
                os.environ[chiave] = valore


def test_lucchetto_impedisce_sincronizzazioni_parallele():
    """
    In produzione gunicorn avvia piu' worker: due sincronizzazioni contemporanee
    non devono partire. Simulato tenendo il lucchetto da un'altra connessione.
    """
    from database import _get_raw_connection
    from services import albo_ocf

    conn = _get_raw_connection()
    cur = conn.cursor()
    cur.execute("SELECT pg_advisory_lock(%s)", (albo_ocf.LUCCHETTO_SYNC,))
    try:
        r = albo_ocf.sincronizza(ELENCO_TEST, zip_bytes=_zip_finto(
            [("GINO", "TESTOTTO", "01/01/1980", "ROMA", "RM", "AZIMUT CAPITAL MANAGEMENT SGR SPA")]))
        assert not r["ok"], "la seconda sincronizzazione non doveva procedere"
        assert "altro processo" in (r["errore"] or "")
    finally:
        cur.execute("SELECT pg_advisory_unlock(%s)", (albo_ocf.LUCCHETTO_SYNC,))
        cur.close(); conn.close()

    from database import get_db
    db = get_db(); _pulisci_test(db); db.close()


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
