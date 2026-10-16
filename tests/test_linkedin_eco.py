"""
Guardrail del collegamento LinkedIn → albo.

Il punto critico è il riconoscimento dei nomi: è l'unico passaggio dove un
errore silenzioso produce una metrica che sembra buona e non lo è, oppure manda
Sabia a parlare con la persona sbagliata. Quindi si prova con nomi veri presi
dall'albo e con i modi in cui LinkedIn li deforma.

Esecuzione:
    venv/bin/python tests/test_linkedin_eco.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PROVA = "zzz-test-linkedin"


def _pulisci():
    from database import get_db
    db = get_db()
    db.execute("DELETE FROM linkedin_interazioni WHERE post = ?", (PROVA,))
    db.commit()
    db.close()


def _un_iscritto():
    """
    Una persona del bacino il cui nome è UNIVOCO in tutto l'albo nazionale,
    secondo le stesse regole del servizio: normalizzato e considerando anche la
    forma «cognome nome».

    Prendere il primo iscritto qualunque non basta: se capita un omonimo — anche
    di un'altra provincia — il servizio risponde correttamente AMBIGUO e il test
    fallisce accusando un difetto che non c'è.
    """
    from services import linkedin_eco as L

    mappa = L._carica_indice(forza=True)
    conteggio, bacino = mappa["conteggio"], mappa["bacino"]

    for chiave in sorted(bacino):
        p = bacino[chiave]
        nome, cognome = (p.get("nome") or "").strip(), (p.get("cognome") or "").strip()
        # Serve un nome di due sole parole: con tre o più, nome e cognome si
        # possono ricomporre in modi diversi e il test non proverebbe ciò che crede.
        if not nome or not cognome or " " in nome or " " in cognome:
            continue
        diretto = L._normalizza(f"{nome} {cognome}")
        inverso = L._normalizza(f"{cognome} {nome}")
        if len(conteggio.get(diretto, ())) == 1 and len(conteggio.get(inverso, ())) == 1:
            return dict(p)
    return None


def test_normalizzazione_regge_accenti_e_punteggiatura():
    from services.linkedin_eco import _normalizza
    assert _normalizza("Nicolò D'Angelo") == "nicolo d angelo"
    assert _normalizza("  MARIA   ROSSI  ") == "maria rossi"
    assert _normalizza("Jean-Luc Picard") == "jean luc picard"
    assert _normalizza("") == ""
    assert _normalizza(None) == ""


def test_riconosce_una_persona_vera_del_bacino():
    from services import linkedin_eco as L

    p = _un_iscritto()
    assert p, "serve almeno un nome univoco nel bacino"
    nome_pieno = f"{p['nome']} {p['cognome']}"

    r = L.riconosci([nome_pieno])[0]
    assert r["esito"] == L.RICONOSCIUTO, r
    assert r["persona"]["chiave"] == p["chiave"]


def test_riconosce_anche_con_nome_e_cognome_invertiti():
    """Su LinkedIn l'ordine non è garantito e alcune esportazioni lo invertono."""
    from services import linkedin_eco as L

    p = _un_iscritto()
    r = L.riconosci([f"{p['cognome']} {p['nome']}"])[0]
    assert r["esito"] == L.RICONOSCIUTO, r
    assert r["persona"]["chiave"] == p["chiave"]


def test_riconosce_con_maiuscole_spazi_e_accenti_sporchi():
    from services import linkedin_eco as L

    p = _un_iscritto()
    sporco = f"  {p['nome'].upper()}   {p['cognome'].lower()}  "
    r = L.riconosci([sporco])[0]
    assert r["esito"] == L.RICONOSCIUTO, r


def test_chi_non_e_nel_bacino_resta_fuori():
    from services import linkedin_eco as L

    esiti = L.riconosci(["Giovanni Inesistente Cognomefinto", "Mario Rossi Nonesiste"])
    assert all(e["esito"] == L.FUORI for e in esiti), esiti
    assert all(e["persona"] is None for e in esiti)


def test_nome_di_una_parola_sola_non_viene_attribuito():
    """Capita con i profili che mostrano solo il nome: attribuirlo sarebbe inventare."""
    from services import linkedin_eco as L

    r = L.riconosci(["Giuseppe"])[0]
    assert r["esito"] == L.FUORI
    assert "incompleto" in r["motivo"]


def test_omonimi_restano_ambigui_e_non_vengono_assegnati():
    """
    Il guardrail principale, provato su dati costruiti apposta.

    Prima questo test cercava due omonimi nell'albo e, se non li trovava,
    passava senza verificare niente: il controllo più importante del servizio
    poteva restare scoperto per mesi senza che nessuno se ne accorgesse. Ora i
    due omonimi li creiamo noi, con chiavi riconoscibili, e li cancelliamo.
    """
    from database import get_db
    from services import albo_ocf, linkedin_eco as L

    chiavi = ("zzz-test-omonimo-1", "zzz-test-omonimo-2")
    db = get_db()
    try:
        for k in chiavi:
            db.execute("""
                INSERT INTO ocf_iscritti (chiave, nome, cognome, anno_nascita, comune,
                                          provincia, regione, rete, elenco, attivo)
                VALUES (?, 'Zelmiro', 'Testoni', 1980, 'Roma', ?, 'Lazio',
                        'Rete Di Prova', 'abilitati', TRUE)
                ON CONFLICT (chiave) DO NOTHING
            """, (k, albo_ocf.PROVINCIA_OPERATIVA))
        db.commit()
        db.close()

        L.invalida_indice()
        r = L.riconosci(["Zelmiro Testoni"])[0]
        assert r["esito"] == L.AMBIGUO, r
        assert r["persona"] is None, "un omonimo non deve mai essere attribuito"
        assert "2" in r["motivo"], r["motivo"]

        # anche con nome e cognome invertiti l'ambiguità resta
        r2 = L.riconosci(["Testoni Zelmiro"])[0]
        assert r2["esito"] == L.AMBIGUO, r2
    finally:
        db = get_db()
        db.execute("DELETE FROM ocf_iscritti WHERE chiave IN (?, ?)", chiavi)
        db.commit()
        db.close()
        L.invalida_indice()


def test_omonimo_fuori_provincia_non_viene_attribuito_a_quello_di_roma():
    """
    Il caso che il filtro per provincia nascondeva: se esiste un omonimo in
    un'altra provincia, il commento non è attribuibile. Caricando solo Roma il
    servizio avrebbe visto un solo nome e assegnato con sicurezza la persona
    sbagliata.
    """
    from database import get_db
    from services import albo_ocf, linkedin_eco as L

    chiavi = ("zzz-test-roma", "zzz-test-milano")
    db = get_db()
    try:
        db.execute("""
            INSERT INTO ocf_iscritti (chiave, nome, cognome, anno_nascita, comune,
                                      provincia, regione, rete, elenco, attivo)
            VALUES (?, 'Osvaldo', 'Pernici', 1975, 'Roma', ?, 'Lazio',
                    'Rete Di Prova', 'abilitati', TRUE)
            ON CONFLICT (chiave) DO NOTHING
        """, (chiavi[0], albo_ocf.PROVINCIA_OPERATIVA))
        db.execute("""
            INSERT INTO ocf_iscritti (chiave, nome, cognome, anno_nascita, comune,
                                      provincia, regione, rete, elenco, attivo)
            VALUES (?, 'Osvaldo', 'Pernici', 1975, 'Milano', 'MI', 'Lombardia',
                    'Rete Di Prova', 'abilitati', TRUE)
            ON CONFLICT (chiave) DO NOTHING
        """, (chiavi[1],))
        db.commit()
        db.close()

        L.invalida_indice()
        r = L.riconosci(["Osvaldo Pernici"])[0]
        assert r["esito"] == L.AMBIGUO, (
            "l'omonimo di Milano deve rendere il nome non attribuibile, "
            f"invece: {r}")
        assert r["persona"] is None
    finally:
        db = get_db()
        db.execute("DELETE FROM ocf_iscritti WHERE chiave IN (?, ?)", chiavi)
        db.commit()
        db.close()
        L.invalida_indice()


def test_iscritto_di_altra_provincia_non_entra_nel_bacino():
    """Chi è nell'albo ma fuori provincia si riconosce e si dichiara, non si conta."""
    from database import get_db
    from services import linkedin_eco as L

    chiave = "zzz-test-solo-milano"
    db = get_db()
    try:
        db.execute("""
            INSERT INTO ocf_iscritti (chiave, nome, cognome, anno_nascita, comune,
                                      provincia, regione, rete, elenco, attivo)
            VALUES (?, 'Ubaldo', 'Scarpellini', 1979, 'Milano', 'MI', 'Lombardia',
                    'Rete Di Prova', 'abilitati', TRUE)
            ON CONFLICT (chiave) DO NOTHING
        """, (chiave,))
        db.commit()
        db.close()

        L.invalida_indice()
        r = L.riconosci(["Ubaldo Scarpellini"])[0]
        assert r["esito"] == L.FUORI, r
        assert "provincia" in r["motivo"], r["motivo"]
    finally:
        db = get_db()
        db.execute("DELETE FROM ocf_iscritti WHERE chiave = ?", (chiave,))
        db.commit()
        db.close()
        L.invalida_indice()


def test_registra_e_conta_solo_persone_distinte():
    from services import linkedin_eco as L

    _pulisci()
    try:
        p = _un_iscritto()
        nome = f"{p['nome']} {p['cognome']}"
        # lo stesso consulente interagisce tre volte: resta una persona sola
        esito = L.registra([nome, nome, nome, "Tizio Nonesistente Qui"],
                           tipo="commento", post=PROVA)
        assert esito["totale"] == 4
        assert esito["nel_bacino"] == 3, esito
        assert esito["fuori"] == 1

        from database import get_db
        db = get_db()
        distinte = db.execute("""
            SELECT COUNT(DISTINCT chiave) AS n FROM linkedin_interazioni
             WHERE post = ? AND chiave IS NOT NULL
        """, (PROVA,)).fetchone()["n"]
        db.close()
        assert distinte == 1, "tre interazioni della stessa persona valgono una persona"
    finally:
        _pulisci()


def test_profili_da_commentare_sono_ordinati_e_spiegati():
    from services import linkedin_eco as L

    profili = L.profili_da_commentare(limite=10)
    assert profili, "il bacino non può essere vuoto"

    indici = [float(p["propensione"]["indice"]) for p in profili]
    assert indici == sorted(indici, reverse=True), "vanno dal più propenso al meno"

    for p in profili:
        c = p["contesto"]
        assert c["angolo"], f"{p['cognome']} non ha un angolo di contatto"
        assert c["temperatura"] in ("alta", "media", "bassa")
        assert c["da_evitare"] and c["da_fare"]
        # Lo strumento prepara il contesto, non scrive il commento al posto suo.
        assert "testo" not in c and "commento" not in c


def test_nessun_profilo_gia_in_pipeline_tra_i_suggeriti():
    from database import get_db
    from services import linkedin_eco as L

    chiavi = {p["chiave"] for p in L.profili_da_commentare(limite=30)}
    if not chiavi:
        return
    db = get_db()
    try:
        segnaposto = ",".join(["?"] * len(chiavi))
        gia = db.execute(
            f"SELECT chiave FROM ocf_dossier WHERE esito = 'in_pipeline' AND chiave IN ({segnaposto})",
            list(chiavi)).fetchall()
    finally:
        db.close()
    assert not gia, f"{len(gia)} profili già in pipeline sono rientrati fra i suggeriti"


if __name__ == "__main__":
    import logging
    import types

    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))
    logging.getLogger("database").setLevel(logging.ERROR)
    logging.getLogger("services.linkedin_eco").setLevel(logging.ERROR)

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
