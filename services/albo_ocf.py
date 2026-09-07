"""
Servizio Albo OCF — sincronizzazione, ricerca e radar movimenti.

Idea di fondo: invece di *campionare* LinkedIn sperando di incrociare un
consulente finanziario, si parte dalla POPOLAZIONE COMPLETA pubblicata da OCF
(~56.000 CF abilitati, aggiornata dall'Organismo) e si filtra per rete e città.

Il secondo uso, più prezioso: confrontando l'elenco di oggi con quello
dell'ultima sincronizzazione si ottengono i PASSAGGI DI RETE nominativi —
chi ha cambiato bandiera, chi è entrato nell'albo, chi ne è uscito. È il
segnale di mobilità che nessuna ricerca per ruolo può dare.

Nota sulle performance: il wrapper `_PgConnection` esegue un SAVEPOINT per ogni
INSERT (serve a emulare `lastrowid`). Su 56.000 righe sarebbe inaccettabile, per
questo la sincronizzazione usa direttamente la connessione psycopg2 grezza con
`execute_values`. Tutto il resto del servizio usa la normale `get_db()`.
"""

import logging
from datetime import date

from psycopg2.extras import execute_values

from connettori import ocf_elenco
from database import _get_raw_connection, get_db

logger = logging.getLogger(__name__)

# Quante righe per batch nell'inserimento massivo
BATCH = 5000


# ──────────────────────────────────────────────────────────────────────────────
# Sincronizzazione
# ──────────────────────────────────────────────────────────────────────────────

def sincronizza(elenco: str = "abilitati", zip_bytes: bytes = None) -> dict:
    """
    Scarica l'elenco ufficiale, lo confronta con lo snapshot in database e
    registra i movimenti. Idempotente: rieseguirla sullo stesso elenco non
    genera movimenti falsi.

    `zip_bytes` permette di passare un archivio già scaricato (usato dai test).
    Ritorna un dizionario di diagnostica; non solleva mai per errori di rete:
    l'errore finisce in `esito`/`errore` e viene registrato in `ocf_sync`.
    """
    esito = {"ok": False, "elenco": elenco, "totale": 0, "nuovi": 0,
             "cambi_rete": 0, "usciti": 0, "data_elenco": None, "errore": None}

    try:
        dati_zip = zip_bytes or ocf_elenco.scarica_zip(elenco)
        d_elenco = ocf_elenco.data_elenco(dati_zip)
        righe = list(ocf_elenco.leggi_iscritti(dati_zip))
    except Exception as e:
        logger.error("OCF sync: download/parsing fallito: %s", e, exc_info=True)
        esito["errore"] = f"Elenco OCF non scaricabile o illeggibile: {e}"
        _registra_sync(esito)
        return esito

    if not righe:
        esito["errore"] = "L'elenco OCF è arrivato vuoto: sincronizzazione annullata."
        _registra_sync(esito)
        return esito

    esito["totale"] = len(righe)
    esito["data_elenco"] = d_elenco.isoformat()

    conn = _get_raw_connection()
    cur = conn.cursor()
    try:
        # 1) Staging temporanea (vive quanto la transazione)
        cur.execute("""
            CREATE TEMP TABLE ocf_stg (
                chiave TEXT PRIMARY KEY, nome TEXT, cognome TEXT, anno_nascita INTEGER,
                comune TEXT, provincia TEXT, regione TEXT, rete TEXT, rete_raw TEXT
            ) ON COMMIT DROP
        """)
        valori = [(r["chiave"], r["nome"], r["cognome"], r["anno_nascita"],
                   r["comune"], r["provincia"], r["regione"], r["rete"], r["rete_raw"])
                  for r in righe]
        for i in range(0, len(valori), BATCH):
            execute_values(
                cur,
                "INSERT INTO ocf_stg (chiave, nome, cognome, anno_nascita, comune, "
                "provincia, regione, rete, rete_raw) VALUES %s ON CONFLICT DO NOTHING",
                valori[i:i + BATCH],
            )

        # È la prima sincronizzazione? Se sì non esistono "movimenti": non
        # sappiamo da quando ciascuno è nella sua rete, sappiamo solo che oggi
        # c'è. Lo dichiariamo con rete_dal_stimata = TRUE.
        # NB: la connessione raw usa RealDictCursor → si legge per nome di colonna
        cur.execute("SELECT COUNT(*) AS n FROM ocf_iscritti WHERE elenco = %s", (elenco,))
        prima_volta = (cur.fetchone()["n"] or 0) == 0

        # 2) Movimenti — solo dalla seconda sincronizzazione in poi
        if not prima_volta:
            # 2a) cambi di rete
            cur.execute("""
                INSERT INTO ocf_movimenti (chiave, nome, cognome, comune, provincia,
                                           tipo, rete_precedente, rete_nuova, data_elenco)
                SELECT s.chiave, s.nome, s.cognome, s.comune, s.provincia,
                       'cambio_rete', i.rete, s.rete, %s
                  FROM ocf_stg s
                  JOIN ocf_iscritti i ON i.chiave = s.chiave AND i.elenco = %s
                 WHERE COALESCE(i.rete,'') <> COALESCE(s.rete,'')
                ON CONFLICT DO NOTHING
            """, (d_elenco, elenco))
            esito["cambi_rete"] = cur.rowcount

            # 2b) nuovi iscritti
            cur.execute("""
                INSERT INTO ocf_movimenti (chiave, nome, cognome, comune, provincia,
                                           tipo, rete_precedente, rete_nuova, data_elenco)
                SELECT s.chiave, s.nome, s.cognome, s.comune, s.provincia,
                       'nuovo', NULL, s.rete, %s
                  FROM ocf_stg s
                  LEFT JOIN ocf_iscritti i ON i.chiave = s.chiave AND i.elenco = %s
                 WHERE i.chiave IS NULL
                ON CONFLICT DO NOTHING
            """, (d_elenco, elenco))
            esito["nuovi"] = cur.rowcount

            # 2c) usciti dall'albo (non più presenti nell'elenco)
            cur.execute("""
                INSERT INTO ocf_movimenti (chiave, nome, cognome, comune, provincia,
                                           tipo, rete_precedente, rete_nuova, data_elenco)
                SELECT i.chiave, i.nome, i.cognome, i.comune, i.provincia,
                       'uscito', i.rete, NULL, %s
                  FROM ocf_iscritti i
                  LEFT JOIN ocf_stg s ON s.chiave = i.chiave
                 WHERE i.elenco = %s AND i.attivo = TRUE AND s.chiave IS NULL
                ON CONFLICT DO NOTHING
            """, (d_elenco, elenco))
            esito["usciti"] = cur.rowcount
            esito["societari"] = _marca_societari(cur, d_elenco)
            esito["cambi_rete"] = max(0, esito["cambi_rete"] - esito["societari"])

        # 3) Aggiorna lo snapshot.
        #    - chi cambia rete: rete_dal = data elenco, rete_dal_stimata = FALSE
        #      (da qui in poi l'anzianità nella rete è un dato osservato, non una stima)
        #    - n_cambi cresce solo sui cambi veri
        cur.execute("""
            INSERT INTO ocf_iscritti (chiave, nome, cognome, anno_nascita, comune,
                    provincia, regione, rete, rete_raw, elenco, attivo, rete_dal,
                    rete_dal_stimata, n_cambi, primo_avvistamento, ultimo_avvistamento)
            SELECT s.chiave, s.nome, s.cognome, s.anno_nascita, s.comune, s.provincia,
                   s.regione, s.rete, s.rete_raw, %s, TRUE, %s, TRUE, 0, %s, %s
              FROM ocf_stg s
            ON CONFLICT (chiave) DO UPDATE SET
                nome = EXCLUDED.nome,
                cognome = EXCLUDED.cognome,
                anno_nascita = EXCLUDED.anno_nascita,
                comune = EXCLUDED.comune,
                provincia = EXCLUDED.provincia,
                regione = EXCLUDED.regione,
                rete_raw = EXCLUDED.rete_raw,
                attivo = TRUE,
                ultimo_avvistamento = EXCLUDED.ultimo_avvistamento,
                n_cambi = ocf_iscritti.n_cambi
                          + CASE WHEN COALESCE(ocf_iscritti.rete,'') <> COALESCE(EXCLUDED.rete,'')
                                 THEN 1 ELSE 0 END,
                rete_dal = CASE WHEN COALESCE(ocf_iscritti.rete,'') <> COALESCE(EXCLUDED.rete,'')
                                THEN EXCLUDED.rete_dal ELSE ocf_iscritti.rete_dal END,
                rete_dal_stimata = CASE WHEN COALESCE(ocf_iscritti.rete,'') <> COALESCE(EXCLUDED.rete,'')
                                        THEN FALSE ELSE ocf_iscritti.rete_dal_stimata END,
                rete = EXCLUDED.rete
        """, (elenco, d_elenco, d_elenco, d_elenco))

        # 4) Marca come non attivi quelli spariti dall'elenco
        cur.execute("""
            UPDATE ocf_iscritti i SET attivo = FALSE
             WHERE i.elenco = %s AND i.attivo = TRUE
               AND NOT EXISTS (SELECT 1 FROM ocf_stg s WHERE s.chiave = i.chiave)
        """, (elenco,))

        conn.commit()
        esito["ok"] = True
        logger.info("OCF sync %s: %d righe, %d nuovi, %d cambi rete, %d usciti",
                    elenco, esito["totale"], esito["nuovi"], esito["cambi_rete"], esito["usciti"])
    except Exception as e:
        conn.rollback()
        logger.error("OCF sync: errore in scrittura: %s", e, exc_info=True)
        esito["errore"] = f"Sincronizzazione non riuscita: {e}"
    finally:
        cur.close()
        conn.close()

    _registra_sync(esito)
    return esito


def _marca_societari(cur, data_elenco) -> int:
    """
    Marca come `societario` i cambi di rete che non sono scelte individuali:
    passaggi interni allo stesso gruppo bancario, oppure flussi di massa da una
    rete all'altra (fusione o cambio di ragione sociale).

    Senza questo passaggio il radar direbbe "635 persone hanno lasciato Deutsche
    Bank" quando in realtà Deutsche Bank è diventata Zurich Bank.
    Ritorna quanti movimenti sono stati marcati.
    """
    from connettori.ocf_elenco import stesso_gruppo

    cur.execute("""
        SELECT rete_precedente, rete_nuova, COUNT(*) AS n
          FROM ocf_movimenti
         WHERE tipo = 'cambio_rete' AND data_elenco = %s
         GROUP BY rete_precedente, rete_nuova
    """, (data_elenco,))
    coppie = [dict(r) for r in cur.fetchall()]
    if not coppie:
        return 0

    cur.execute("SELECT rete, COUNT(*) AS n FROM ocf_iscritti WHERE attivo = TRUE GROUP BY rete")
    popolazione = {r["rete"]: r["n"] for r in cur.fetchall()}

    da_marcare = []
    for c in coppie:
        r0, r1, n = c["rete_precedente"], c["rete_nuova"], c["n"]
        di_massa = (n >= SOGLIA_PERSONE_SOCIETARIA
                    and n / max(1, popolazione.get(r0, 1)) >= SOGLIA_QUOTA_SOCIETARIA)
        if di_massa or stesso_gruppo(r0 or "", r1 or ""):
            da_marcare.append((r0, r1))

    marcati = 0
    for r0, r1 in da_marcare:
        cur.execute("""
            UPDATE ocf_movimenti SET societario = TRUE
             WHERE tipo = 'cambio_rete' AND data_elenco = %s
               AND rete_precedente IS NOT DISTINCT FROM %s
               AND rete_nuova IS NOT DISTINCT FROM %s
        """, (data_elenco, r0, r1))
        marcati += cur.rowcount
    if marcati:
        logger.info("Sync OCF: %d movimenti marcati come societari", marcati)
    return marcati


def _registra_sync(esito: dict) -> None:
    """Scrive una riga di storico in ocf_sync. Non solleva mai."""
    try:
        db = get_db()
        db.execute(
            "INSERT INTO ocf_sync (elenco, data_elenco, totale, nuovi, cambi_rete, "
            "usciti, esito, errore) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (esito.get("elenco"), esito.get("data_elenco"), esito.get("totale", 0),
             esito.get("nuovi", 0), esito.get("cambi_rete", 0), esito.get("usciti", 0),
             "ok" if esito.get("ok") else "errore", esito.get("errore")),
        )
        db.commit()
        db.close()
    except Exception as e:  # pragma: no cover
        logger.warning("OCF sync: storico non registrato (%s)", e)


# ──────────────────────────────────────────────────────────────────────────────
# Ricerca nell'albo
# ──────────────────────────────────────────────────────────────────────────────

# Reti da cui ha senso reclutare: tutto tranne Fideuram (siamo noi) e chi non ha
# mandato. Usato quando il chiamante non specifica una rete.
RETE_PROPRIA = "Fideuram"


def cerca(rete: str = "", comune: str = "", provincia: str = "", regione: str = "",
          eta_min: int = None, eta_max: int = None, escludi_propria: bool = True,
          solo_con_rete: bool = True, limite: int = 100, offset: int = 0) -> dict:
    """
    Interroga lo snapshot dell'albo. Zero chiamate esterne, zero costi:
    è una query su tabella locale.

    Ritorna {"totale": N, "profili": [...]} dove `totale` è il conteggio pieno
    (non la pagina), così l'interfaccia può dire onestamente quanti ce ne sono.
    """
    dove = ["attivo = TRUE"]
    par = []

    if rete:
        dove.append("LOWER(rete) LIKE LOWER(?)")
        par.append(f"%{rete.strip()}%")
    elif escludi_propria:
        dove.append("rete <> ?")
        par.append(RETE_PROPRIA)
    if solo_con_rete:
        dove.append("COALESCE(rete, '') <> ''")
    if comune:
        dove.append("LOWER(comune) = LOWER(?)")
        par.append(comune.strip())
    if provincia:
        dove.append("UPPER(provincia) = UPPER(?)")
        par.append(provincia.strip()[:2])
    if regione:
        dove.append("LOWER(regione) = LOWER(?)")
        par.append(regione.strip())
    anno = date.today().year
    if eta_min:
        dove.append("anno_nascita IS NOT NULL AND anno_nascita <= ?")
        par.append(anno - int(eta_min))
    if eta_max:
        dove.append("anno_nascita IS NOT NULL AND anno_nascita >= ?")
        par.append(anno - int(eta_max))

    where = " AND ".join(dove)
    db = get_db()
    try:
        totale = db.execute(f"SELECT COUNT(*) AS n FROM ocf_iscritti WHERE {where}",
                            par).fetchone()["n"]
        righe = db.execute(
            f"""SELECT chiave, nome, cognome, anno_nascita, comune, provincia, regione,
                       rete, rete_dal, rete_dal_stimata, n_cambi
                  FROM ocf_iscritti WHERE {where}
                 ORDER BY cognome, nome LIMIT ? OFFSET ?""",
            par + [int(limite), int(offset)],
        ).fetchall()
    finally:
        db.close()

    profili = []
    for r in righe:
        d = dict(r)
        d["eta"] = (anno - d["anno_nascita"]) if d.get("anno_nascita") else None
        d["nome_completo"] = f"{d.get('nome','')} {d.get('cognome','')}".strip()
        profili.append(d)
    return {"totale": totale, "profili": profili}


def verifica(nome: str, cognome: str) -> dict:
    """
    Verifica se un nominativo risulta nell'albo e con quale rete.
    Serve alla triangolazione: conferma che il profilo LinkedIn è davvero un CF
    e dice in che rete è OGGI (LinkedIn spesso è fermo a due lavori fa).

    Ritorna {"trovato": bool, "ambiguo": bool, "iscritti": [...]}.
    L'omonimia è dichiarata, non nascosta: con due "Marco Rossi" non si sceglie
    a caso, si segnala.
    """
    nome = (nome or "").strip()
    cognome = (cognome or "").strip()
    if not cognome:
        return {"trovato": False, "ambiguo": False, "iscritti": [],
                "nota": "Cognome mancante: impossibile verificare nell'albo."}

    db = get_db()
    try:
        righe = db.execute(
            """SELECT nome, cognome, comune, provincia, regione, rete, anno_nascita,
                      rete_dal, rete_dal_stimata, n_cambi, attivo
                 FROM ocf_iscritti
                WHERE LOWER(cognome) = LOWER(?) AND (? = '' OR LOWER(nome) = LOWER(?))
                ORDER BY attivo DESC, cognome LIMIT 10""",
            (cognome, nome, nome),
        ).fetchall()
    finally:
        db.close()

    iscritti = [dict(r) for r in righe]
    return {
        "trovato": bool(iscritti),
        "ambiguo": len(iscritti) > 1,
        "iscritti": iscritti,
        "nota": ("Nessun iscritto con questo nominativo: il profilo potrebbe non essere "
                 "un consulente finanziario abilitato, oppure essere registrato con un "
                 "nome diverso." if not iscritti else ""),
    }


def verifica_batch(persone: list) -> dict:
    """
    Verifica in un colpo solo un elenco di (nome, cognome) — pensata per arricchire
    i risultati di una ricerca LinkedIn con il dato ufficiale.

    Ritorna {"cognome|nome": {...}} con la rete REALE di oggi. Serve perché
    l'headline LinkedIn è spesso ferma a due lavori fa, mentre l'albo no.
    Le omonimie non vengono risolte a caso: se ci sono più iscritti con lo stesso
    nome e cognome, il record riporta ambiguo=True e nessuna rete.
    """
    coppie = [(str(n or "").strip().lower(), str(c or "").strip().lower())
              for n, c in persone if str(c or "").strip()]
    if not coppie:
        return {}

    cognomi = sorted({c for _n, c in coppie})
    segnaposto = ",".join(["?"] * len(cognomi))
    db = get_db()
    try:
        righe = db.execute(
            f"""SELECT nome, cognome, comune, provincia, rete, anno_nascita,
                       rete_dal, rete_dal_stimata, n_cambi
                  FROM ocf_iscritti
                 WHERE attivo = TRUE AND LOWER(cognome) IN ({segnaposto})""",
            cognomi,
        ).fetchall()
    finally:
        db.close()

    per_coppia = {}
    for r in righe:
        k = f"{(r['cognome'] or '').lower()}|{(r['nome'] or '').lower()}"
        per_coppia.setdefault(k, []).append(dict(r))

    esito = {}
    for nome, cognome in coppie:
        trovati = per_coppia.get(f"{cognome}|{nome}", [])
        chiave = f"{cognome}|{nome}"
        if not trovati:
            esito[chiave] = {"trovato": False, "ambiguo": False}
        elif len(trovati) > 1:
            esito[chiave] = {"trovato": True, "ambiguo": True, "quanti": len(trovati)}
        else:
            t = trovati[0]
            esito[chiave] = {
                "trovato": True, "ambiguo": False, "rete": t.get("rete") or "",
                "comune": t.get("comune") or "", "provincia": t.get("provincia") or "",
                "n_cambi": t.get("n_cambi") or 0,
                "anno_nascita": t.get("anno_nascita"),
            }
    return esito


def movimenti(tipo: str = "", giorni: int = 90, rete: str = "", provincia: str = "",
              limite: int = 200, includi_societari: bool = False) -> list:
    """
    Passaggi di rete rilevati dal confronto fra elenchi.
    `tipo`: 'cambio_rete' | 'nuovo' | 'uscito' (vuoto = tutti).
    Di default esclude i passaggi societari (fusioni, rinomine, giri interni al
    gruppo): non sono persone che hanno scelto di cambiare.
    """
    dove = ["rilevato_il >= CURRENT_DATE - CAST(? AS INTEGER)"]
    if not includi_societari:
        dove.append("societario IS NOT TRUE")
    par = [int(giorni)]
    if tipo:
        dove.append("tipo = ?")
        par.append(tipo)
    if rete:
        dove.append("(LOWER(COALESCE(rete_precedente,'')) LIKE LOWER(?) "
                    "OR LOWER(COALESCE(rete_nuova,'')) LIKE LOWER(?))")
        par += [f"%{rete}%", f"%{rete}%"]
    if provincia:
        dove.append("UPPER(provincia) = UPPER(?)")
        par.append(provincia[:2])

    db = get_db()
    try:
        righe = db.execute(
            f"""SELECT * FROM ocf_movimenti WHERE {' AND '.join(dove)}
                 ORDER BY rilevato_il DESC, cognome LIMIT ?""",
            par + [int(limite)],
        ).fetchall()
    finally:
        db.close()
    return [dict(r) for r in righe]


# ──────────────────────────────────────────────────────────────────────────────
# Storico: ricostruzione dei passaggi passati dagli elenchi archiviati
# ──────────────────────────────────────────────────────────────────────────────

# Un flusso rete→rete che sposta almeno questa quota della rete di partenza (e
# almeno questo numero di persone) è una fusione o un cambio di ragione sociale,
# non un insieme di scelte individuali.
SOGLIA_QUOTA_SOCIETARIA = 0.25
SOGLIA_PERSONE_SOCIETARIA = 20


def _confronta(prima: dict, dopo: dict, data_dopo, etichetta_a: str, etichetta_b: str) -> list:
    """
    Passaggi fra due snapshot {chiave: record}. Ogni movimento porta il flag
    `societario`, così le riorganizzazioni non inquinano né le statistiche né
    le etichette di un eventuale modello.
    """
    from connettori.ocf_elenco import stesso_gruppo

    comuni = set(prima) & set(dopo)
    popolazione = {}
    for k in comuni:
        r = prima[k].get("rete") or ""
        if r:
            popolazione[r] = popolazione.get(r, 0) + 1

    grezzi = []
    for k in comuni:
        r0 = prima[k].get("rete") or ""
        r1 = dopo[k].get("rete") or ""
        if r0 and r1 and r0 != r1:
            grezzi.append((k, r0, r1))

    conteggio = {}
    for _k, r0, r1 in grezzi:
        conteggio[(r0, r1)] = conteggio.get((r0, r1), 0) + 1

    movimenti_out = []
    for k, r0, r1 in grezzi:
        n = conteggio[(r0, r1)]
        di_massa = (n >= SOGLIA_PERSONE_SOCIETARIA
                    and n / max(1, popolazione.get(r0, 1)) >= SOGLIA_QUOTA_SOCIETARIA)
        rec = dopo[k]
        movimenti_out.append({
            "chiave": k, "nome": rec.get("nome", ""), "cognome": rec.get("cognome", ""),
            "comune": rec.get("comune", ""), "provincia": rec.get("provincia", ""),
            "rete_precedente": r0, "rete_nuova": r1, "data_elenco": data_dopo,
            "societario": bool(di_massa or stesso_gruppo(r0, r1)),
            "intervallo": f"{etichetta_a}→{etichetta_b}",
        })
    return movimenti_out


def importa_storico(limite_snapshot: int = 12) -> dict:
    """
    Ricostruisce i passaggi di rete passati dagli elenchi archiviati dal Wayback
    Machine, e chiude la catena con lo snapshot corrente in database.

    Non modifica lo stato corrente dei consulenti (che resta l'elenco ufficiale
    più recente): scrive solo movimenti datati e aggiorna `n_cambi`.
    Idempotente: i movimenti già presenti per la stessa persona e la stessa data
    non vengono riscritti.
    """
    from connettori import ocf_storico

    esito = {"ok": False, "snapshot": 0, "movimenti": 0, "societari": 0,
             "regioni": [], "intervalli": [], "errore": None}

    voci = ocf_storico.elenchi_archiviati()[:limite_snapshot]
    if not voci:
        esito["errore"] = ("Nessun elenco storico disponibile nell'archivio "
                           "(Wayback Machine irraggiungibile o nulla di archiviato).")
        return esito

    snapshot = []
    for voce in voci:
        try:
            persone, regioni = ocf_storico.leggi_snapshot(ocf_storico.scarica(voce))
        except Exception as e:
            logger.warning("Storico OCF: copia del %s non leggibile (%s)", voce["data"], e)
            continue
        if len(persone) < 1000:      # copia troppo mutila per essere utile
            continue
        snapshot.append({"data": voce["data"], "persone": persone, "regioni": regioni})

    if not snapshot:
        esito["errore"] = "Le copie archiviate non contengono dati utilizzabili."
        return esito

    # Le regioni recuperabili sono un sottoinsieme: i confronti valgono solo lì.
    regioni_comuni = set(snapshot[0]["regioni"])
    for s in snapshot[1:]:
        regioni_comuni &= set(s["regioni"])
    esito["regioni"] = sorted(regioni_comuni)
    esito["snapshot"] = len(snapshot)

    # Ultimo anello: lo stato di oggi, ristretto alle stesse regioni
    db = get_db()
    try:
        righe = db.execute(
            "SELECT chiave, nome, cognome, comune, provincia, regione, rete "
            "FROM ocf_iscritti WHERE attivo = TRUE"
        ).fetchall()
    finally:
        db.close()
    regioni_up = {r.upper() for r in regioni_comuni}
    oggi = {r["chiave"]: dict(r) for r in righe
            if (r["regione"] or "").upper() in regioni_up}
    if oggi:
        snapshot.append({"data": date.today(), "persone": oggi, "regioni": sorted(regioni_comuni)})

    movimenti_totali = []
    for a, b in zip(snapshot, snapshot[1:]):
        m = _confronta(a["persone"], b["persone"], b["data"],
                       a["data"].isoformat(), b["data"].isoformat())
        movimenti_totali += m
        veri = sum(1 for x in m if not x["societario"])
        esito["intervalli"].append({
            "da": a["data"].isoformat(), "a": b["data"].isoformat(),
            "passaggi": veri, "societari": len(m) - veri,
            "base": len({k for k, v in a["persone"].items() if v.get("rete")}),
        })

    scritti = _scrivi_movimenti(movimenti_totali)
    esito["movimenti"] = scritti
    esito["societari"] = sum(1 for m in movimenti_totali if m["societario"])
    esito["ok"] = True
    logger.info("Storico OCF: %d snapshot, %d movimenti scritti", len(snapshot), scritti)
    return esito


def _scrivi_movimenti(movimenti: list) -> int:
    """Scrive i movimenti evitando i duplicati (stessa persona, stessa data)."""
    if not movimenti:
        return 0
    conn = _get_raw_connection()
    cur = conn.cursor()
    try:
        # NB: execute_values esegue a pagine, quindi cur.rowcount riporta solo
        # l'ultima pagina: il conteggio vero si fa confrontando il totale prima/dopo.
        cur.execute("SELECT COUNT(*) AS n FROM ocf_movimenti")
        prima = cur.fetchone()["n"] or 0
        for i in range(0, len(movimenti), BATCH):
            blocco = movimenti[i:i + BATCH]
            valori = [(m["chiave"], m["nome"], m["cognome"], m["comune"], m["provincia"],
                       "cambio_rete", m["rete_precedente"], m["rete_nuova"],
                       m["data_elenco"], m["societario"], m["data_elenco"])
                      for m in blocco]
            execute_values(cur, """
                INSERT INTO ocf_movimenti (chiave, nome, cognome, comune, provincia, tipo,
                                           rete_precedente, rete_nuova, data_elenco,
                                           societario, rilevato_il)
                SELECT v.chiave, v.nome, v.cognome, v.comune, v.provincia, v.tipo,
                       v.rete_precedente, v.rete_nuova, v.data_elenco, v.societario, v.rilevato_il
                  FROM (VALUES %s) AS v (chiave, nome, cognome, comune, provincia, tipo,
                        rete_precedente, rete_nuova, data_elenco, societario, rilevato_il)
                 WHERE NOT EXISTS (
                    SELECT 1 FROM ocf_movimenti m
                     WHERE m.chiave = v.chiave AND m.data_elenco = v.data_elenco::date)
            """, valori)

        cur.execute("SELECT COUNT(*) AS n FROM ocf_movimenti")
        scritti = (cur.fetchone()["n"] or 0) - prima

        # n_cambi = quanti passaggi VERI risultano nello storico
        cur.execute("""
            UPDATE ocf_iscritti i SET n_cambi = s.n
              FROM (SELECT chiave, COUNT(*) AS n FROM ocf_movimenti
                     WHERE tipo = 'cambio_rete' AND societario IS NOT TRUE
                     GROUP BY chiave) s
             WHERE s.chiave = i.chiave AND i.n_cambi <> s.n
        """)
        conn.commit()
    except Exception as e:
        conn.rollback()
        logger.error("Storico OCF: scrittura fallita: %s", e, exc_info=True)
        raise
    finally:
        cur.close()
        conn.close()
    return scritti


def serve_aggiornamento(giorni: int = 7) -> bool:
    """
    True se l'ultimo aggiornamento RIUSCITO è più vecchio di `giorni`
    (o se non ce n'è mai stato uno).
    """
    db = get_db()
    try:
        r = db.execute(
            """SELECT MAX(eseguito_il) AS ultimo FROM ocf_sync
                WHERE esito = 'ok' AND elenco = 'abilitati'"""
        ).fetchone()
    except Exception:
        return False   # nel dubbio non scarichiamo nulla
    finally:
        db.close()

    ultimo = (r or {}).get("ultimo")
    if not ultimo:
        return True
    try:
        giorno = str(ultimo)[:10]
        anno, mese, gg = (int(x) for x in giorno.split("-"))
        return (date.today() - date(anno, mese, gg)).days >= giorni
    except Exception:
        return False


def squadre_in_movimento(min_persone: int = 2, giorni: int = 1826, limite: int = 40) -> list:
    """
    Grappoli di passaggi: più consulenti della STESSA rete e della STESSA provincia
    che nella stessa finestra sono andati alla STESSA destinazione.

    Non è un caso: negli elenchi 2022 i passaggi a grappolo sono fino a 12 volte
    più frequenti di quanto sarebbero se ognuno scegliesse per conto suo. È la
    firma del gruppo che segue il proprio responsabile.

    Per ogni squadra restituisce anche `colleghi_rimasti`: quante persone della
    stessa rete e zona sono ancora lì. Sono i nomi da chiamare — con l'avvertenza
    che i dati NON dimostrano che seguano (vedi `colleghi_rimasti()`).
    """
    db = get_db()
    try:
        righe = db.execute("""
            SELECT rete_precedente, rete_nuova, provincia, data_elenco,
                   COUNT(*) AS persone,
                   STRING_AGG(nome || ' ' || cognome, ' · ' ORDER BY cognome) AS nomi,
                   STRING_AGG(DISTINCT comune, ', ')                          AS comuni
              FROM ocf_movimenti
             WHERE tipo = 'cambio_rete' AND societario IS NOT TRUE
               AND rilevato_il >= CURRENT_DATE - CAST(? AS INTEGER)
               AND COALESCE(provincia, '') <> ''
             GROUP BY rete_precedente, rete_nuova, provincia, data_elenco
            HAVING COUNT(*) >= ?
             ORDER BY COUNT(*) DESC, data_elenco DESC
             LIMIT ?
        """, (int(giorni), int(min_persone), int(limite))).fetchall()
    finally:
        db.close()

    squadre = []
    for r in righe:
        d = dict(r)
        d["colleghi_rimasti"] = _conta_colleghi(d["rete_precedente"], d["provincia"])
        squadre.append(d)
    return squadre


def _conta_colleghi(rete: str, provincia: str) -> int:
    db = get_db()
    try:
        r = db.execute(
            "SELECT COUNT(*) AS n FROM ocf_iscritti "
            "WHERE attivo = TRUE AND rete = ? AND provincia = ?",
            (rete, provincia)).fetchone()
    finally:
        db.close()
    return (r or {}).get("n", 0)


def colleghi_rimasti(rete: str, provincia: str, limite: int = 200) -> dict:
    """
    Chi è ancora nella rete che ha appena perso un gruppo, nella stessa provincia.

    ⚠️ Onestà sul segnale: sugli elenchi 2022 il fatto che ≥2 colleghi se ne siano
    andati NON risulta predire in modo affidabile che gli altri li seguano
    (lift misurati: 1,1x · 3,6x · 0,0x su pochissimi eventi = rumore). Quello che i
    dati mostrano con forza è che la squadra si muove INSIEME, nella stessa
    finestra — non a scaglioni. Quindi questa lista va lavorata SUBITO dopo la
    rilevazione, non fra tre mesi, e va presa come "occasione di contatto con un
    motivo concreto", non come previsione.
    """
    db = get_db()
    try:
        mossi = {r["chiave"] for r in db.execute(
            "SELECT chiave FROM ocf_movimenti WHERE tipo = 'cambio_rete'").fetchall()}
        righe = db.execute("""
            SELECT chiave, nome, cognome, anno_nascita, comune, provincia, rete, n_cambi
              FROM ocf_iscritti
             WHERE attivo = TRUE AND rete = ? AND provincia = ?
             ORDER BY comune, cognome LIMIT ?
        """, (rete, provincia, int(limite))).fetchall()
    finally:
        db.close()

    anno = date.today().year
    profili = []
    for r in righe:
        d = dict(r)
        if d["chiave"] in mossi:
            continue
        d["eta"] = (anno - d["anno_nascita"]) if d.get("anno_nascita") else None
        d["nome_completo"] = f"{d.get('nome','')} {d.get('cognome','')}".strip()
        profili.append(d)
    return {"totale": len(profili), "profili": profili}


def mobilita_per_rete(min_consulenti: int = 200) -> list:
    """
    Per ogni rete: quanti consulenti ha oggi e quanti l'hanno lasciata secondo lo
    storico dei passaggi VERI (esclusi fusioni, rinomine e giri interni al gruppo).

    È la risposta operativa a "da dove conviene pescare": non tutte le reti
    perdono persone allo stesso ritmo, e la differenza fra la più mobile e la più
    stabile è di diverse volte.
    """
    db = get_db()
    try:
        righe = db.execute("""
            SELECT i.rete,
                   COUNT(DISTINCT i.chiave) AS consulenti,
                   COALESCE(u.usciti, 0)    AS usciti,
                   COALESCE(e.entrati, 0)   AS entrati
              FROM ocf_iscritti i
              LEFT JOIN (SELECT rete_precedente AS rete, COUNT(*) AS usciti
                           FROM ocf_movimenti
                          WHERE tipo = 'cambio_rete' AND societario IS NOT TRUE
                          GROUP BY 1) u ON u.rete = i.rete
              LEFT JOIN (SELECT rete_nuova AS rete, COUNT(*) AS entrati
                           FROM ocf_movimenti
                          WHERE tipo = 'cambio_rete' AND societario IS NOT TRUE
                          GROUP BY 1) e ON e.rete = i.rete
             -- Denominatore ristretto alle regioni per cui esiste storico: gli
             -- elenchi archiviati sono troncati e coprono ~11 regioni su 22.
             -- Senza questo vincolo le reti del Nord sembrerebbero più mobili
             -- solo perché lì abbiamo più osservazioni.
             WHERE i.attivo = TRUE AND COALESCE(i.rete,'') <> ''
               AND i.regione IN (SELECT DISTINCT i2.regione FROM ocf_iscritti i2
                                  WHERE i2.chiave IN (SELECT chiave FROM ocf_movimenti))
             GROUP BY i.rete, u.usciti, e.entrati
             HAVING COUNT(DISTINCT i.chiave) >= ?
             ORDER BY COALESCE(u.usciti,0)::float / COUNT(DISTINCT i.chiave) DESC
        """, (int(min_consulenti),)).fetchall()
    finally:
        db.close()

    fuori = []
    for r in righe:
        d = dict(r)
        d["tasso_uscita"] = round(d["usciti"] / d["consulenti"] * 100, 1) if d["consulenti"] else 0
        d["saldo"] = d["entrati"] - d["usciti"]
        fuori.append(d)
    return fuori


def statistiche() -> dict:
    """Riepilogo per la pagina Albo: copertura, ultima sincronizzazione, top reti."""
    db = get_db()
    try:
        tot = db.execute("SELECT COUNT(*) AS n FROM ocf_iscritti WHERE attivo = TRUE").fetchone()["n"]
        reti = [dict(r) for r in db.execute(
            """SELECT rete, COUNT(*) AS n FROM ocf_iscritti
                WHERE attivo = TRUE AND COALESCE(rete,'') <> ''
                GROUP BY rete ORDER BY n DESC LIMIT 15""").fetchall()]
        ultima = db.execute(
            "SELECT * FROM ocf_sync ORDER BY id DESC LIMIT 1").fetchone()
        n_mov = db.execute(
            "SELECT COUNT(*) AS n FROM ocf_movimenti "
            "WHERE tipo = 'cambio_rete' AND societario IS NOT TRUE").fetchone()["n"]
    finally:
        db.close()
    return {"totale": tot, "reti": reti,
            "ultima_sync": dict(ultima) if ultima else None,
            "cambi_rete_totali": n_mov}
