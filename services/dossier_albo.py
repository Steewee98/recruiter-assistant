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
import re
import unicodedata

logger = logging.getLogger(__name__)


def _parole(testo: str) -> list:
    """Minuscole, senza accenti, senza punteggiatura: 'D\'Alò' → ['d', 'alo']."""
    piatto = unicodedata.normalize("NFKD", testo or "")
    piatto = "".join(c for c in piatto if not unicodedata.combining(c))
    return [p for p in re.split(r"[^a-z0-9]+", piatto.lower()) if p]


def _nome_uguale(nome_cercato: str, cognome_cercato: str,
                 nome_trovato: str, cognome_trovato: str) -> bool:
    """
    Verifica stretta del nominativo: ogni parola cercata deve comparire INTERA
    fra le parole del profilo.

    Il confronto per sottostringhe (`"rossi" in "gianmario rossini"`) fa passare
    persone diverse: cercando «Mario Rossi» accetterebbe «Gianmario Rossini» e il
    dossier finirebbe attribuito a uno sconosciuto. Su un elenco di 56.000
    nominativi il caso non è teorico.

    Il cognome deve corrispondere per intero; del nome basta la prima parola,
    perché LinkedIn spesso riporta solo quella (o aggiunge secondi nomi).
    """
    cognomi_cercati = _parole(cognome_cercato)
    parole_trovate = set(_parole(nome_trovato)) | set(_parole(cognome_trovato))
    if not cognomi_cercati or not parole_trovate:
        return False
    if not all(c in parole_trovate for c in cognomi_cercati):
        return False
    nomi_cercati = _parole(nome_cercato)
    if not nomi_cercati:
        return True
    return nomi_cercati[0] in parole_trovate


def _cerca_linkedin(nome: str, cognome: str, rete: str = "", comune: str = "",
                    max_wait: int = 70) -> tuple:
    """
    Cerca su LinkedIn il profilo di questa persona.
    Ritorna (profilo, nota, altri_profili_omonimi).

    La verifica del nome è obbligatoria: l'actor restituisce comunque qualcosa,
    e un omonimo qualsiasi allegato al dossier sarebbe peggio di nessun profilo.
    """
    # Import locale: routes.ricerca importa a sua volta i servizi
    from routes.ricerca import cerca_apify, normalizza_profilo

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
        if _nome_uguale(nome, cognome, p.get("nome", ""), p.get("cognome", "")):
            candidati.append(p)
        else:
            scartati.append(f"{p.get('nome','')} {p.get('cognome','')}".strip())

    if not candidati:
        return None, ("Nessuna corrispondenza sicura su LinkedIn"
                      + (f" (trovati invece: {', '.join(x for x in scartati[:3] if x)})"
                         if scartati else "")
                      + "."), []

    def segnali(p):
        """
        Due segnali distinti, perché dicono cose diverse:
          • azienda   → il profilo cita la rete in cui l'albo lo colloca
          • mestiere  → il profilo dice che fa consulenza finanziaria

        Tenerli separati evita l'errore di considerare "verificato" un omonimo
        solo perché la sua headline contiene una parola generica: un consulente
        informatico o un dipendente qualsiasi di quella banca non sono la persona
        che cerchiamo. La conferma piena richiede entrambi i segnali.
        """
        testo = " ".join([p.get("ruolo", ""), p.get("azienda", ""),
                          p.get("sommario", "")]).lower()
        # Parole della ragione sociale che non identificano nulla da sole
        generiche = {"banca", "bank", "banco", "spa", "group", "gruppo", "italia",
                     "italy", "private", "financial", "advisors", "capital",
                     "management", "sgr", "sim", "investments", "premier"}
        azienda = any(len(par) > 3 and par not in generiche and par in testo
                      for par in _parole(rete or ""))
        # Mestieri: espressioni specifiche, non prefissi come "consulen" che
        # prendono anche "consulente informatico"
        mestiere = any(t in testo for t in (
            "consulente finanziario", "consulenza finanziaria", "consulente patrimoniale",
            "financial advisor", "financial advisory", "private banker", "private banking",
            "wealth manag", "wealth advis", "gestore patrimon", "promotore finanziario",
            "family banker", "relationship manager", "consulente del credito",
            "investment advisor", "asset manag",
        ))
        return azienda, mestiere

    def punteggio(p):
        azienda, mestiere = segnali(p)
        testo = " ".join([p.get("ruolo", ""), p.get("location", "")]).lower()
        extra = (1 if comune and comune.lower() in testo else 0) + (1 if p.get("ruolo") else 0)
        return (3 if azienda else 0) * 10 + (2 if mestiere else 0) * 10 + extra

    candidati.sort(key=punteggio, reverse=True)
    migliore = candidati[0]
    altri = [c.get("linkedin", "") for c in candidati[1:4] if c.get("linkedin")]
    azienda, mestiere = segnali(migliore)

    if not azienda and not mestiere:
        quanti = (f"{len(candidati)} profili con questo nome e nessuno"
                  if len(candidati) > 1 else "Profilo trovato ma nessun segnale")
        return migliore, (f"{quanti} che risulti del settore finanziario: "
                          "da verificare a mano prima di usarlo."), altri

    # Parità: due omonimi ugualmente plausibili non si risolvono col caso.
    pari = [c for c in candidati if punteggio(c) == punteggio(migliore)]
    if len(pari) > 1:
        return migliore, (f"{len(pari)} profili con lo stesso nome e gli stessi segnali: "
                          "il primo è una scelta arbitraria, da verificare."), altri

    if not (azienda and mestiere):
        manca = "la rete di appartenenza" if mestiere else "il mestiere"
        return migliore, (f"Corrispondenza probabile ma non piena: nel profilo non compare "
                          f"{manca}."), altri
    return migliore, "", altri


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
        # Anche il parsing di una risposta Apify malformata deve restare confinato
        # qui: il resto del dossier è locale e non ha motivo di cadere con lui.
        try:
            prof_li, nota, omonimi = _cerca_linkedin(nome, cognome, profilo.get("rete", ""),
                                                     profilo.get("comune", ""))
        except Exception as e:
            logger.error("Dossier: ricerca LinkedIn fallita per %s %s: %s",
                         nome, cognome, e, exc_info=True)
            prof_li, nota, omonimi = None, f"Ricerca LinkedIn non riuscita: {e}", []
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
