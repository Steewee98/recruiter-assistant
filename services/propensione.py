"""
Coefficiente di propensione al cambio rete.

Non è un punteggio inventato: i parametri sono STIMATI dai passaggi realmente
osservati negli elenchi OCF, e il metodo è stato validato fuori campione —
stimando i tassi sui soli elenchi 2022 e verificandoli sui passaggi 2023-2026,
mai visti in fase di stima:

    ordinando per coefficiente, nei primi 500 nomi il 32,6% ha poi cambiato rete
    contro l'8,6% della popolazione → lift 3,8x   (ordinamento casuale: 0,84x)

Cosa pesa, secondo i dati:
  • LA RETE fa quasi tutto il lavoro (lift 3,5x da sola). Fra la rete più mobile
    e la più stabile c'è un fattore 3.
  • L'ETÀ aggiunge poco ma in modo coerente (da sola 1,2-1,5x): sotto i 35 ci si
    muove ~1,3 volte la media, sopra i 65 circa la metà.
  • Quello che NON funziona, misurato e scartato: essersi già mossi in passato
    (lift 1,00x) e avere colleghi usciti di recente dalla stessa rete e provincia
    (0,92x). Sembravano le variabili più promettenti: non lo sono.

Il coefficiente è quindi deliberatamente semplice — due fattori — perché è tutto
ciò che i dati sostengono. Aggiungere variabili senza segnale non lo renderebbe
più preciso, solo più difficile da spiegare a chi deve usarlo.
"""

import logging
from datetime import date

from database import get_db

logger = logging.getLogger(__name__)

# Forza dello smoothing verso la media di mercato: una rete con pochi consulenti
# osservati non deve schizzare in cima per due passaggi fortuiti.
# Scelto per validazione, non a occhio: fuori campione il lift resta piatto per
# qualunque valore fra 10 e 400 (3,61x-3,75x nei primi 500). A parità di resa si
# prende il valore più prudente, perché con poco smoothing la cima della lista
# viene occupata da banche minuscole con due o tre uscite — statisticamente
# fragili e operativamente inutili, visto che servono nomi in quantità.
SMOOTHING = 200.0

# Ampiezza in anni della finestra storica osservata (gen 2022 → set 2026):
# serve solo a esprimere i tassi su base annua.
ANNI_OSSERVATI = 4.6

FASCE = ("<35", "35-44", "45-54", "55-64", "65+")

# Moltiplicatori per fascia d'età, misurati su una COORTE CHIUSA: la popolazione
# presente nell'elenco di gennaio 2022, seguita per i dodici mesi successivi.
#
# Perché non si stimano dal database come i tassi per rete: nell'archivio la
# fotografia della popolazione è quella di oggi, e comprende chi si è iscritto
# all'albo a metà della finestra osservata. Quei nuovi entrati hanno avuto meno
# tempo per cambiare rete, sono quasi tutti giovani, e trascinano artificialmente
# in basso il tasso della fascia <35 (misurato così darebbe 0,51x invece di 1,29x:
# segno rovesciato). Con una coorte chiusa il problema non esiste — tutti sono
# osservati per lo stesso periodo.
#
# Il guadagno che l'età porta è comunque modesto: nella validazione fuori
# campione il lift passa da 3,54x (sola rete) a 3,80x (rete × età).
MOLTIPLICATORI_ETA = {
    "<35": 1.29, "35-44": 1.06, "45-54": 1.01, "55-64": 1.01, "65+": 0.54,
    "sconosciuta": 1.0,
}


def fascia_eta(eta) -> str:
    if not eta:
        return "sconosciuta"
    if eta < 35:
        return "<35"
    if eta < 45:
        return "35-44"
    if eta < 55:
        return "45-54"
    if eta < 65:
        return "55-64"
    return "65+"


def stima_parametri() -> dict:
    """
    Ricava dai dati in database i tassi di uscita per rete e i moltiplicatori per
    fascia d'età. Va richiamata quando arrivano nuovi movimenti.

    Il denominatore è ristretto alle regioni per cui esiste storico: altrove non
    abbiamo osservato nulla, e contarle come "nessuna uscita" falserebbe i tassi.
    """
    anno_ora = date.today().year
    # Età di riferimento per chi NON si è mosso: quella a metà della finestra
    # osservata, non quella di oggi. Su quasi cinque anni la differenza sposta
    # migliaia di persone da una fascia all'altra.
    anno_medio = anno_ora - int(ANNI_OSSERVATI / 2)

    db = get_db()
    try:
        # Regioni con storico vero: quelle in cui abbiamo osservato passaggi in
        # numero non trascurabile. Una regione con due movimenti "di rimbalzo"
        # (persone che si sono trasferite) non è coperta.
        regioni = [r["regione"] for r in db.execute("""
            SELECT i.regione, COUNT(*) AS n
              FROM ocf_movimenti m JOIN ocf_iscritti i ON i.chiave = m.chiave
             WHERE m.tipo = 'cambio_rete' AND m.societario IS NOT TRUE
             GROUP BY i.regione HAVING COUNT(*) >= 20
        """).fetchall()]
        if not regioni:
            return {"pronto": False, "motivo": "Nessun passaggio storico in archivio."}

        segnaposto = ",".join(["?"] * len(regioni))

        # ── Per rete ──────────────────────────────────────────────────────────
        # ATTENZIONE al verso: il passaggio va attribuito alla rete di PARTENZA.
        # Usare la rete attuale significherebbe contare chi ha lasciato BNL per
        # Fideuram come "un mobile di Fideuram", cioè invertire il segnale.
        usciti = {r["rete"]: r["n"] for r in db.execute(f"""
            SELECT m.rete_precedente AS rete, COUNT(DISTINCT m.chiave) AS n
              FROM ocf_movimenti m JOIN ocf_iscritti i ON i.chiave = m.chiave
             WHERE m.tipo = 'cambio_rete' AND m.societario IS NOT TRUE
               AND COALESCE(m.rete_precedente,'') <> ''
               AND i.regione IN ({segnaposto})
             GROUP BY 1
        """, regioni).fetchall()}

        attivi = {r["rete"]: r["n"] for r in db.execute(f"""
            SELECT rete, COUNT(*) AS n FROM ocf_iscritti
             WHERE attivo = TRUE AND COALESCE(rete,'') <> ''
               AND regione IN ({segnaposto})
             GROUP BY 1
        """, regioni).fetchall()}

        # ── Per fascia d'età ──────────────────────────────────────────────────
        # Numeratore e denominatore vanno costruiti sulla STESSA definizione di
        # età, altrimenti il confronto è truccato: qui entrambi usano l'età a
        # metà della finestra osservata. (Usare l'età al momento del passaggio
        # per chi si è mosso e quella di oggi per gli altri ribaltava il segno
        # del risultato: i giovani finivano artificialmente fra i meno mobili.)
        mossi_eta = {r["fascia"]: r["n"] for r in db.execute(f"""
            SELECT CASE WHEN i.anno_nascita IS NULL THEN 'sconosciuta'
                        WHEN {anno_medio} - i.anno_nascita < 35 THEN '<35'
                        WHEN {anno_medio} - i.anno_nascita < 45 THEN '35-44'
                        WHEN {anno_medio} - i.anno_nascita < 55 THEN '45-54'
                        WHEN {anno_medio} - i.anno_nascita < 65 THEN '55-64'
                        ELSE '65+' END AS fascia,
                   COUNT(DISTINCT m.chiave) AS n
              FROM ocf_movimenti m JOIN ocf_iscritti i ON i.chiave = m.chiave
             WHERE m.tipo = 'cambio_rete' AND m.societario IS NOT TRUE
               AND i.regione IN ({segnaposto})
             GROUP BY 1
        """, regioni).fetchall()}

        esposti_eta = {r["fascia"]: r["n"] for r in db.execute(f"""
            SELECT CASE WHEN anno_nascita IS NULL THEN 'sconosciuta'
                        WHEN {anno_medio} - anno_nascita < 35 THEN '<35'
                        WHEN {anno_medio} - anno_nascita < 45 THEN '35-44'
                        WHEN {anno_medio} - anno_nascita < 55 THEN '45-54'
                        WHEN {anno_medio} - anno_nascita < 65 THEN '55-64'
                        ELSE '65+' END AS fascia,
                   COUNT(*) AS n
              FROM ocf_iscritti
             WHERE attivo = TRUE AND COALESCE(rete,'') <> ''
               AND regione IN ({segnaposto})
             GROUP BY 1
        """, regioni).fetchall()}
    finally:
        db.close()

    # Esposti a una rete = chi c'è oggi + chi l'ha lasciata (che oggi risulta altrove)
    reti_tutte = set(attivi) | set(usciti)
    tot_esposti = sum(attivi.get(r, 0) + usciti.get(r, 0) for r in reti_tutte) or 1
    tot_mossi = sum(usciti.values())
    base = tot_mossi / tot_esposti

    reti = {}
    for r in reti_tutte:
        esposti = attivi.get(r, 0) + usciti.get(r, 0)
        mossi = usciti.get(r, 0)
        if esposti < 10:
            continue
        # media pesata fra il tasso osservato e la media di mercato: una rete
        # piccola non deve schizzare in cima per due passaggi fortuiti
        reti[r] = {"tasso": (mossi + base * SMOOTHING) / (esposti + SMOOTHING),
                   "osservati": esposti, "mossi": mossi}

    # `esposti_eta` conta già tutti gli iscritti attivi, compresi quelli che si
    # sono mossi (che restano nell'albo, solo con un'altra rete): non va sommato
    # `mossi_eta`, sarebbe un doppio conteggio.
    base_eta = (sum(mossi_eta.values()) / sum(esposti_eta.values())
                if sum(esposti_eta.values()) else base)
    eta = {}
    for f, esposti in esposti_eta.items():
        if esposti < 50:
            continue
        t = mossi_eta.get(f, 0) / esposti
        eta[f] = {"moltiplicatore": round(t / base_eta, 3) if base_eta else 1.0,
                  "osservati": esposti, "mossi": mossi_eta.get(f, 0)}

    # `eta` qui è la stima grezza dal database: viene esposta solo come
    # diagnostica (è distorta dai nuovi iscritti, vedi MOLTIPLICATORI_ETA).
    return {"pronto": True, "base": base, "reti": reti, "eta_osservata": eta,
            "eta": {f: {"moltiplicatore": m} for f, m in MOLTIPLICATORI_ETA.items()},
            "regioni_osservate": sorted(regioni),
            "esposti": tot_esposti, "mossi": tot_mossi}


_cache = {"parametri": None}


def parametri(ricalcola: bool = False) -> dict:
    if ricalcola or _cache["parametri"] is None:
        _cache["parametri"] = stima_parametri()
    return _cache["parametri"]


def coefficiente(rete: str, eta=None, par: dict = None) -> dict:
    """
    Coefficiente per un singolo consulente.

    Ritorna probabilità annua stimata, quante volte la media di mercato vale, e
    il MOTIVO in chiaro. Il motivo non è un abbellimento: un punteggio che chi
    telefona non sa spiegare non viene usato.
    """
    par = par or parametri()
    if not par.get("pronto"):
        return {"disponibile": False, "motivo": par.get("motivo", "Parametri non stimabili.")}

    base = par["base"]
    info_rete = par["reti"].get(rete or "")
    tasso_rete = info_rete["tasso"] if info_rete else base
    f = fascia_eta(eta)
    molt = MOLTIPLICATORI_ETA.get(f, 1.0)

    tasso = tasso_rete * molt
    annuo = tasso / ANNI_OSSERVATI
    volte = tasso / base if base else 1.0

    perche = []
    if info_rete:
        perche.append(
            f"{rete}: se n'è andato il {info_rete['mossi'] / max(1, info_rete['osservati']) * 100:.0f}% "
            f"in {ANNI_OSSERVATI:.0f} anni"
            + (" (pochi dati)" if info_rete["osservati"] < 100 else ""))
    else:
        perche.append(f"{rete or 'rete ignota'}: nessuno storico, uso la media di mercato")
    if f != "sconosciuta":
        if molt >= 1.15:
            perche.append(f"{f} anni: si muovono {molt:.1f}x la media")
        elif molt <= 0.85:
            perche.append(f"{f} anni: si muovono {molt:.1f}x la media (meno mobili)")
        else:
            perche.append(f"{f} anni: come la media")

    return {"disponibile": True, "indice": round(volte, 2),
            "probabilita_annua": round(annuo * 100, 1),
            "probabilita_periodo": round(tasso * 100, 1),
            "perche": perche, "fascia": f}


def ordina(profili: list) -> list:
    """Aggiunge il coefficiente a una lista di profili e la ordina dal più propenso."""
    par = parametri()
    for p in profili:
        p["propensione"] = coefficiente(p.get("rete", ""), p.get("eta"), par)
    profili.sort(key=lambda x: -(x["propensione"].get("indice") or 0))
    return profili


def valuta(k_list=(500, 1000, 2000)) -> dict:
    """
    Verifica di onestà: quanto bene il coefficiente ordina davvero.

    Ordina la popolazione osservata per coefficiente e misura, nei primi k, la
    quota di chi RISULTA essersi mosso. Attenzione: qui i parametri sono stimati
    sugli stessi passaggi che si stanno misurando, quindi il lift è ottimistico
    per costruzione — è un controllo di sanità, non una prova. La prova vera è
    stata fatta fuori campione (stima sul 2022, verifica sul 2023-2026: 3,8x).
    """
    par = parametri(ricalcola=True)
    if not par.get("pronto"):
        return {"disponibile": False, "motivo": par.get("motivo")}

    db = get_db()
    try:
        segnaposto = ",".join(["?"] * len(par["regioni_osservate"]))
        # Popolazione com'era all'inizio della finestra: chi è ancora nella sua
        # rete, più chi l'ha lasciata (con la rete che aveva ALLORA).
        fermi = db.execute(f"""
            SELECT i.rete, i.anno_nascita, FALSE AS mosso
              FROM ocf_iscritti i
             WHERE i.attivo = TRUE AND COALESCE(i.rete,'') <> ''
               AND i.regione IN ({segnaposto})
               AND i.chiave NOT IN (SELECT chiave FROM ocf_movimenti
                                     WHERE tipo = 'cambio_rete' AND societario IS NOT TRUE)
        """, par["regioni_osservate"]).fetchall()
        mossi = db.execute(f"""
            SELECT DISTINCT ON (m.chiave) m.rete_precedente AS rete,
                   i.anno_nascita, TRUE AS mosso
              FROM ocf_movimenti m JOIN ocf_iscritti i ON i.chiave = m.chiave
             WHERE m.tipo = 'cambio_rete' AND m.societario IS NOT TRUE
               AND COALESCE(m.rete_precedente,'') <> ''
               AND i.regione IN ({segnaposto})
             ORDER BY m.chiave, m.data_elenco
        """, par["regioni_osservate"]).fetchall()
        righe = list(fermi) + list(mossi)
    finally:
        db.close()

    anno = date.today().year - int(ANNI_OSSERVATI / 2)
    punteggiati = []
    for r in righe:
        eta = (anno - r["anno_nascita"]) if r["anno_nascita"] else None
        c = coefficiente(r["rete"], eta, par)
        punteggiati.append((c["indice"], bool(r["mosso"])))
    punteggiati.sort(key=lambda x: -x[0])

    n = len(punteggiati)
    positivi = sum(1 for _s, m in punteggiati if m)
    tasso_base = positivi / n if n else 0
    risultati = []
    for k in k_list:
        if k > n:
            continue
        presi = sum(1 for _s, m in punteggiati[:k] if m)
        risultati.append({"k": k, "mossi": presi,
                          "percentuale": round(presi / k * 100, 1),
                          "lift": round((presi / k) / tasso_base, 2) if tasso_base else 0})
    return {"disponibile": True, "popolazione": n, "mossi": positivi,
            "tasso_base": round(tasso_base * 100, 1), "risultati": risultati}
