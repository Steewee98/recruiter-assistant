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
            """, (d_elenco, elenco))
            esito["usciti"] = cur.rowcount

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
              limite: int = 200) -> list:
    """
    Passaggi di rete rilevati dal confronto fra elenchi.
    `tipo`: 'cambio_rete' | 'nuovo' | 'uscito' (vuoto = tutti).
    """
    dove = ["rilevato_il >= CURRENT_DATE - CAST(? AS INTEGER)"]
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
            "SELECT COUNT(*) AS n FROM ocf_movimenti WHERE tipo = 'cambio_rete'").fetchone()["n"]
    finally:
        db.close()
    return {"totale": tot, "reti": reti,
            "ultima_sync": dict(ultima) if ultima else None,
            "cambi_rete_totali": n_mov}
