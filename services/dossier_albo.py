"""
Dossier di un consulente dell'albo OCF.

Mette insieme, per una singola persona, tutto quello che sappiamo prima di
telefonare:

  1. ANAGRAFICA UFFICIALE (albo OCF) — rete, comune, età, numero di passaggi
     osservati. È il dato certo: viene dall'Organismo, non da un profilo curato
     dall'interessato.
  2. COEFFICIENTE di propensione al cambio, con il motivo in chiaro.
  3. PROFILO LINKEDIN, cercato per nome sulla rete di appartenenza e verificato
     (se il nome non corrisponde il profilo viene scartato, non "adattato").
  4. SINTESI AI facoltativa.

Due scelte deliberate:

• L'ordine dei passaggi non è casuale. LinkedIn costa (una ricerca Apify per
  persona) ed è la parte che può fallire; l'albo no. Quindi il dossier viene
  costruito comunque, e LinkedIn lo arricchisce se c'è. Un dossier senza
  LinkedIn resta utile: nome, rete, città, età e propensione bastano per una
  prima telefonata.

• La sintesi AI è l'ultimo strato e non è mai bloccante. Se l'API non risponde
  (credito esaurito, rate limit) il dossier si chiude lo stesso e dichiara che
  il commento manca: un tool che smette di funzionare perché un fornitore è giù
  è un tool che non si può usare.
"""

import logging

logger = logging.getLogger(__name__)


def _cerca_linkedin(nome: str, cognome: str, rete: str = "", comune: str = "",
                    max_wait: int = 70) -> tuple:
    """
    Cerca su LinkedIn il profilo di questa persona.
    Ritorna (profilo, nota, altri_profili_omonimi).

    La verifica del nome è obbligatoria: l'actor restituisce comunque qualcosa,
    e un omonimo qualsiasi allegato al dossier sarebbe peggio di nessun profilo.
    """
    # Import locale: routes.ricerca importa a sua volta i servizi
    from routes.ricerca import cerca_apify, normalizza_profilo, _nome_corrisponde

    nome_completo = f"{nome} {cognome}".strip()
    if not cognome:
        return None, "Cognome mancante: ricerca LinkedIn non eseguita.", []

    # Ricerca per NOME con i filtri dedicati dell'actor (firstNames/lastNames):
    # la ricerca testuale restituiva persone a caso.
    items, errore = cerca_apify(
        ruolo="", citta="", azienda=rete or "", max_items=8, max_wait=max_wait,
        cerca_nome=(nome, cognome),
    )
    # Senza risultati riprova senza il vincolo di azienda: sull'albo la rete è
    # quella ufficiale, ma su LinkedIn la persona può averla scritta in mille modi.
    if not errore and not items and rete:
        items, errore = cerca_apify(
            ruolo="", citta="", max_items=8, max_wait=max_wait,
            cerca_nome=(nome, cognome),
        )
    if errore:
        return None, f"LinkedIn non raggiungibile: {errore}", []
    if not items:
        return None, "Nessun profilo LinkedIn trovato per questo nominativo.", []

    # Fra più omonimi non si prende il primo che capita: si sceglie quello con
    # i segnali giusti (rete di appartenenza, termini del settore, città), e se
    # nessuno ne ha si dichiara l'incertezza invece di indovinare.
    candidati, scartati = [], []
    for item in items:
        if not isinstance(item, dict):
            continue
        p = normalizza_profilo(item)
        if _nome_corrisponde(nome_completo, p.get("nome", ""), p.get("cognome", "")):
            candidati.append(p)
        else:
            scartati.append(f"{p.get('nome','')} {p.get('cognome','')}".strip())

    if not candidati:
        return None, ("Nessuna corrispondenza sicura su LinkedIn"
                      + (f" (trovati invece: {', '.join(x for x in scartati[:3] if x)})"
                         if scartati else "")
                      + "."), []

    def segnale_dominio(p):
        """Punti che dicono 'è davvero un consulente finanziario di quella rete'."""
        testo = " ".join([p.get("ruolo", ""), p.get("azienda", ""),
                          p.get("sommario", "")]).lower()
        punti = 0
        for parola in (rete or "").lower().split():
            if len(parola) > 3 and parola in testo:
                punti += 3
        if any(t in testo for t in ("consulen", "financial", "private bank", "wealth",
                                    "banker", "patrimon", "advisor", "investiment",
                                    "gestore", "promotore")):
            punti += 2
        return punti

    def punteggio(p):
        """Ordinamento: il segnale di dominio conta, il resto è solo spareggio."""
        testo = " ".join([p.get("ruolo", ""), p.get("location", "")]).lower()
        extra = (1 if comune and comune.lower() in testo else 0) + (1 if p.get("ruolo") else 0)
        return segnale_dominio(p) * 10 + extra

    candidati.sort(key=punteggio, reverse=True)
    migliore = candidati[0]

    # Il criterio dell'incertezza è il SEGNALE DI DOMINIO, non il punteggio
    # totale: "ha un ruolo scritto" non distingue un consulente finanziario da
    # un HR specialist omonimo. Senza segnali di settore il profilo va marcato
    # come da verificare, anche quando è l'unico candidato.
    if segnale_dominio(migliore) == 0:
        altri = [c.get("linkedin", "") for c in candidati[1:4] if c.get("linkedin")]
        quanti = (f"{len(candidati)} profili con questo nome e nessuno"
                  if len(candidati) > 1 else "Profilo trovato ma nessun segnale")
        return migliore, (f"{quanti} che risulti del settore finanziario: "
                          "da verificare a mano prima di usarlo."), altri
    if len(candidati) > 1:
        altri = [c.get("linkedin", "") for c in candidati[1:4] if c.get("linkedin")]
        return migliore, "", altri
    return migliore, "", []


def costruisci(profilo: dict, con_linkedin: bool = True, con_ai: bool = True) -> dict:
    """
    Dossier completo per un profilo dell'albo.

    `profilo` è una riga come quelle restituite da albo_ocf.cerca():
    nome, cognome, rete, comune, provincia, eta, n_cambi.
    Non solleva mai: ogni strato che fallisce lascia una nota e il dossier procede.
    """
    from services import albo_ocf, propensione

    nome = (profilo.get("nome") or "").strip()
    cognome = (profilo.get("cognome") or "").strip()
    if not cognome:
        return {"ok": False, "errore": "Profilo senza cognome."}

    dossier = {
        "ok": True,
        "nome_completo": f"{nome} {cognome}".strip(),
        "albo": {
            "rete": profilo.get("rete", ""),
            "comune": profilo.get("comune", ""),
            "provincia": profilo.get("provincia", ""),
            "eta": profilo.get("eta"),
            "n_cambi": profilo.get("n_cambi") or 0,
        },
        "note": [],
    }

    # 1) Coefficiente di propensione (locale, gratuito)
    try:
        dossier["propensione"] = propensione.coefficiente(
            profilo.get("rete", ""), profilo.get("eta"))
    except Exception as e:
        logger.warning("Dossier: coefficiente non calcolabile (%s)", e)
        dossier["note"].append("Coefficiente di propensione non disponibile.")

    # 2) Storia dei passaggi già osservati per questa persona
    try:
        dossier["passaggi"] = _passaggi_persona(nome, cognome)
    except Exception as e:
        logger.warning("Dossier: storico passaggi non leggibile (%s)", e)
        dossier["passaggi"] = []

    # 3) Chi altro se n'è andato dalla sua rete, nella sua provincia:
    #    è il gancio della telefonata, non un dato statistico.
    try:
        gruppi = albo_ocf.squadre_in_movimento(
            min_persone=2, limite=10, solo_rete=profilo.get("rete", ""),
            solo_provincia=profilo.get("provincia", ""))
        # Solo chi è USCITO dalla sua rete: che altri siano arrivati nella sua
        # banca non è un argomento per convincerlo ad andarsene. E i gruppi
        # passati a Fideuram vanno per primi: sono il gancio migliore.
        gruppi = [g for g in gruppi if g["rete_precedente"] == profilo.get("rete", "")]
        gruppi.sort(key=lambda g: (not g.get("verso_di_noi"), -g["persone"]))
        dossier["contesto_rete"] = gruppi[:4]
    except Exception:
        dossier["contesto_rete"] = []

    # 4) LinkedIn (costa: una ricerca Apify)
    if con_linkedin:
        prof_li, nota, omonimi = _cerca_linkedin(nome, cognome, profilo.get("rete", ""),
                                                 profilo.get("comune", ""))
        if prof_li:
            dossier["linkedin"] = {
                "url": prof_li.get("linkedin", ""),
                "ruolo": prof_li.get("ruolo", ""),
                "azienda": prof_li.get("azienda", ""),
                "location": prof_li.get("location", ""),
                "sommario": prof_li.get("sommario", ""),
                "omonimi": omonimi,
            }
            if nota:
                dossier["note"].append(nota)
            # L'albo è più aggiornato del profilo: se non coincidono, dirlo.
            rete_albo = (profilo.get("rete") or "").lower()
            azienda_li = (prof_li.get("azienda") or "").lower()
            if rete_albo and azienda_li and rete_albo not in azienda_li:
                dossier["note"].append(
                    f"Su LinkedIn risulta «{prof_li.get('azienda')}», nell'albo «{profilo.get('rete')}»: "
                    "l'albo è il dato ufficiale e più aggiornato.")
        else:
            dossier["linkedin"] = None
            dossier["note"].append(nota)
    else:
        dossier["linkedin"] = None

    # 5) Sintesi AI — ultimo strato, mai bloccante
    if con_ai:
        try:
            dossier["sintesi"] = _sintesi_ai(dossier)
        except Exception as e:
            from ai_helpers import messaggio_errore_ai
            dossier["sintesi"] = None
            dossier["note"].append(f"Sintesi non generata — {messaggio_errore_ai(e)}")
    else:
        dossier["sintesi"] = None

    return dossier


def _passaggi_persona(nome: str, cognome: str) -> list:
    """Passaggi di rete già osservati per questo nominativo (esclusi quelli societari)."""
    from database import get_db
    db = get_db()
    try:
        righe = db.execute("""
            SELECT rete_precedente, rete_nuova, data_elenco, finestra_giorni
              FROM ocf_movimenti
             WHERE tipo = 'cambio_rete' AND societario IS NOT TRUE
               AND LOWER(cognome) = LOWER(?) AND LOWER(nome) = LOWER(?)
             ORDER BY data_elenco
        """, (cognome, nome)).fetchall()
    finally:
        db.close()
    return [dict(r) for r in righe]


def _sintesi_ai(dossier: dict) -> str:
    """
    Due o tre righe operative: perché chiamarlo e da cosa partire.
    Riceve solo i fatti già raccolti — l'AI commenta, non cerca.
    """
    from ai_helpers import CLAUDE_MODEL, _chiama_api

    a = dossier["albo"]
    p = dossier.get("propensione") or {}
    li = dossier.get("linkedin") or {}
    contesto = dossier.get("contesto_rete") or []

    fatti = [
        f"Nome: {dossier['nome_completo']}",
        f"Rete attuale (albo OCF): {a['rete']}",
        f"Zona: {a['comune']} ({a['provincia']})",
        f"Età: {a['eta']}" if a.get("eta") else "Età: non nota",
        f"Passaggi di rete già osservati: {a['n_cambi']}",
    ]
    if p.get("disponibile"):
        fatti.append(f"Propensione stimata al cambio: {p['indice']}x la media di mercato "
                     f"({p['probabilita_annua']}% l'anno). Motivo: {'; '.join(p.get('perche', []))}")
    if li:
        fatti.append(f"LinkedIn: {li.get('ruolo','')} presso {li.get('azienda','')}. "
                     f"{(li.get('sommario') or '')[:300]}")
    for c in contesto[:2]:
        fatti.append(f"Contesto: {c['persone']} colleghi di {c['rete_precedente']} "
                     f"({c['provincia']}) sono passati a {c['rete_nuova']}.")

    system = (
        "Sei l'assistente di un recruiter di Banca Fideuram che deve chiamare "
        "consulenti finanziari di altre reti.\n"
        "Scrivi in italiano, al massimo 3 frasi, senza elenchi puntati.\n"
        "Dì: (1) perché questa persona merita una chiamata adesso, (2) da quale "
        "argomento partire.\n"
        "Usa SOLO i fatti forniti. Se un dato manca, non inventarlo e non "
        "riempire con frasi generiche di cortesia."
    )
    risposta = _chiama_api("dossier_albo", {
        "model": CLAUDE_MODEL,
        "max_tokens": 300,
        "system": system,
        "messages": [{"role": "user", "content": "\n".join(fatti)}],
    })
    return risposta.content[0].text.strip() if risposta.content else ""
