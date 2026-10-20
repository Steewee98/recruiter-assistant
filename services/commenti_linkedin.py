"""Trova i post dei profili che vogliamo reclutare e prepara una proposta di commento.

Perché esiste
-------------
Con il feed per interesse, da un profilo piccolo la strada più corta verso un
consulente non è pubblicare: è farsi vedere sotto i suoi post. Ma aprire LinkedIn
e cercare a mano chi ha pubblicato qualcosa, fra duecento profili, non lo fa
nessuno per più di tre giorni.

Il contratto
------------
Il programma propone, Sabia decide, la pubblicazione è manuale:

    scarico (Apify) → filtro → proposta di commento → APPROVAZIONE DI SABIA → copia-incolla

Nessun commento esce da qui senza che una persona l'abbia letto. Non è una
cautela formale: sotto il post di un consulente che vuoi reclutare, un commento
che suona finto costa più di quanto il sistema costruisca in un mese.

Quanto costa
------------
Lo scraper si paga a risultato: ~0,002 $ per post trovato, 0,001 $ per una
ricerca a vuoto, e accetta al massimo 10 profili per chiamata. Con ~130 profili
monitorabili e una passata al giorno siamo intorno al dollaro al mese.

Chi scrive il testo
-------------------
Un commento pubblico è un testo che leggono persone fuori da Axiom: lo scrive
Argon tramite `copywriter.scrivi`. Dove il copywriter non è installato (per
esempio sul server) si ripiega su Claude, ma la colonna `autore_testo` lo
registra sempre, così si sa chi ha scritto cosa.
"""

import json
import logging
import os
import re
import sys
import urllib.request
from datetime import datetime, timedelta

from database import get_db

log = logging.getLogger(__name__)

ACTOR = "harvestapi~linkedin-post-search"
APIFY_BASE = "https://api.apify.com/v2"

# L'actor rifiuta liste più lunghe: non è un parametro nostro, è il suo limite.
PROFILI_PER_CHIAMATA = 10

# Gli stati della pipeline in cui una persona è ancora viva: su «Chiuso» non si
# commenta più niente. «Da contattare» è il valore predefinito della colonna,
# quindi se lo prende chi viene inserito a mano: lasciarlo fuori escludeva
# proprio i candidati aggiunti uno per uno, che sono i più curati.
STATI_VIVI = ("Da contattare", "Da valutare", "Richiesta Inviata", "Messaggio Inviato")

# Un post più vecchio di così è acqua passata: commentarlo fa l'effetto opposto.
GIORNI_UTILI = 14

# Sotto questa lunghezza non c'è niente da commentare («Esemplare», un'immagine
# senza testo, un link secco).
TESTO_MINIMO = 150

# Un commento che funziona sotto il post di un altro sta in una o due frasi.
# Oltre, diventa un intervento: sposta l'attenzione dal post a chi commenta, ed è
# il modo più rapido per sembrare uno che cerca visibilità invece di uno che ha
# qualcosa da dire.
MAX_COMMENTO = 220
MAX_FRASI = 2


# ──────────────────────────────────────────────────────────────────────────
#  Scarico
# ──────────────────────────────────────────────────────────────────────────

def profili_da_seguire(limite: int = None) -> list:
    """I candidati vivi con un indirizzo LinkedIn utilizzabile.

    I profili salvati come identificativo interno (/in/ACwA…) vengono esclusi:
    è la forma che restituisce lo scraper dei profili, e la ricerca dei post
    non la accetta. Fingere di poterli seguire produrrebbe solo ricerche a vuoto
    pagate.
    """
    db = get_db()
    try:
        segnaposto = ",".join("?" for _ in STATI_VIVI)
        sql = (f"SELECT id, nome, cognome, azienda, ruolo_attuale, profilo_linkedin, punteggio "
               f"FROM candidati WHERE stato IN ({segnaposto}) "
               f"AND profilo_linkedin IS NOT NULL AND TRIM(profilo_linkedin) <> '' "
               f"AND profilo_linkedin NOT LIKE ? ORDER BY punteggio DESC NULLS LAST")
        righe = db.execute(sql, (*STATI_VIVI, "%/in/ACwA%")).fetchall()
        fuori = [dict(r) for r in righe]
        return fuori[:limite] if limite else fuori
    finally:
        db.close()


def _chiama_apify(urls: list, giorni: int) -> list:
    token = os.getenv("APIFY_API_KEY")
    if not token:
        raise RuntimeError("Manca APIFY_API_KEY: senza chiave non si può scaricare niente.")

    limite = "24h" if giorni <= 1 else ("week" if giorni <= 7 else "month")
    payload = {
        "authorUrls": urls,
        "postedLimit": limite,
        "maxPosts": 40,
        "sortBy": "date",
        "profileScraperMode": "short",
    }
    req = urllib.request.Request(
        f"{APIFY_BASE}/acts/{ACTOR}/run-sync-get-dataset-items?token={token}",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=300) as r:
        return json.loads(r.read())


def _quando(grezzo) -> datetime:
    """La data del post. L'actor la restituisce come timestamp in millisecondi
    dentro un oggetto, oppure come stringa: si accettano entrambe."""
    if isinstance(grezzo, dict):
        ts = grezzo.get("timestamp")
        if ts:
            try:
                return datetime.fromtimestamp(int(ts) / 1000)
            except (ValueError, OSError, TypeError):
                return None
        grezzo = grezzo.get("date") or grezzo.get("postedAt")
    if isinstance(grezzo, str):
        testo = grezzo.strip()
        # fromisoformat non digerisce la «Z»: la si traduce nel fuso che indica.
        # Prima si tagliava la stringa a 26 caratteri, ma su una data con sei
        # decimali quel taglio mangiava proprio la Z e la data diventava
        # illeggibile: il post restava senza data e scavalcava il controllo
        # sull'anzianità invece di essere scartato.
        if testo.endswith("Z"):
            testo = testo[:-1] + "+00:00"
        try:
            d = datetime.fromisoformat(testo)
            # Le date si confrontano con datetime.now(), che non porta fuso:
            # si riporta tutto all'ora locale e si toglie l'informazione.
            return d.astimezone().replace(tzinfo=None) if d.tzinfo else d
        except ValueError:
            pass
        for fmt in ("%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z",
                    "%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%d/%m/%Y"):
            try:
                d = datetime.strptime(testo, fmt)
                return d.astimezone().replace(tzinfo=None) if d.tzinfo else d
            except ValueError:
                continue
    return None


def _conta(post: dict, campo: str) -> int:
    eng = post.get("engagement")
    if isinstance(eng, dict):
        for k in (campo, campo + "s", "num" + campo.capitalize()):
            if isinstance(eng.get(k), int):
                return eng[k]
    v = post.get("num" + campo.capitalize())
    return v if isinstance(v, int) else 0


def _motivo_scarto(post: dict, testo: str, quando: datetime) -> str:
    """Perché un post non merita un commento. Restituisce None se invece lo merita.

    Tutti controlli deterministici: non serve un modello per sapere che una
    pubblicità di una banca non è il posto dove Sabia deve farsi vedere.
    """
    autore = post.get("author") or {}
    # Le pagine aziendali non hanno un identificativo personale: un commento
    # sotto la pubblicità di una banca non parla a nessuno che vogliamo assumere.
    if not autore.get("publicIdentifier"):
        return "è una pagina aziendale, non una persona"
    if post.get("repostedBy") or post.get("isRepost"):
        return "è una condivisione, non un contenuto suo"
    if len(testo) < TESTO_MINIMO:
        return f"troppo corto ({len(testo)} battute): non c'è niente su cui dire qualcosa"
    if quando and quando < datetime.now() - timedelta(days=GIORNI_UTILI):
        return f"pubblicato più di {GIORNI_UTILI} giorni fa"
    return None


def _punteggio(reazioni: int, commenti: int) -> float:
    """Quanto conviene commentare qui.

    Il momento buono è quando il post ha già pubblico ma pochi commenti: si
    viene letti e non si finisce in coda a venti risposte. Un post con molte
    reazioni e zero commenti vale più di uno con poche reazioni e zero commenti,
    ma i commenti già presenti pesano in negativo.
    """
    return round(reazioni / (1 + commenti * 3), 2)


def scarica(giorni: int = 7, limite_profili: int = None) -> dict:
    """Una passata completa: scarica, filtra, salva i post nuovi.

    Restituisce il riepilogo di quello che è successo, compreso quanto è costato
    in termini di risultati pagati — il dato che serve per decidere se la
    frequenza è sostenibile.
    """
    profili = profili_da_seguire(limite_profili)
    if not profili:
        return {"ok": False, "errore": "Nessun profilo da seguire.", "nuovi": 0}

    per_url = {(p["profilo_linkedin"] or "").rstrip("/").lower(): p for p in profili}
    esito = {"ok": True, "profili": len(profili), "post_trovati": 0,
             "nuovi": 0, "scartati": 0, "falliti": 0, "chiamate": 0,
             "lotti_riusciti": 0, "lotti_falliti": 0, "errore": None}

    db = get_db()
    try:
        for i in range(0, len(profili), PROFILI_PER_CHIAMATA):
            lotto = [p["profilo_linkedin"] for p in profili[i:i + PROFILI_PER_CHIAMATA]]
            esito["chiamate"] += 1
            try:
                risultati = _chiama_apify(lotto, giorni)
            except Exception as e:
                log.error("Scarico post: lotto %s fallito: %s", i // PROFILI_PER_CHIAMATA, e)
                esito["lotti_falliti"] += 1
                esito["errore"] = str(e)
                continue
            esito["lotti_riusciti"] += 1

            esito["post_trovati"] += len(risultati)
            for p in risultati:
                # In PostgreSQL un errore lascia la transazione abortita: senza
                # un punto di ripristino, un post malformato farebbe fallire
                # tutti quelli dopo e cancellerebbe quelli già scritti prima,
                # che però risulterebbero contati come salvati.
                db.execute("SAVEPOINT singolo_post")
                try:
                    nuovo = _salva(db, p, per_url, esito)
                    db.execute("RELEASE SAVEPOINT singolo_post")
                    if nuovo:
                        esito["nuovi"] += 1
                except Exception as e:
                    db.execute("ROLLBACK TO SAVEPOINT singolo_post")
                    db.execute("RELEASE SAVEPOINT singolo_post")
                    esito["falliti"] = esito.get("falliti", 0) + 1
                    log.warning("Post non salvato: %s", e)
        db.commit()
    finally:
        db.close()

    # Una ricerca in cui nessuna chiamata è andata a buon fine non è una ricerca
    # senza risultati: è una ricerca fallita. Dire «0 post nuovi» nasconderebbe
    # una chiave scaduta o lo scraper giù, e si continuerebbe a premere il
    # pulsante credendo che non ci sia niente da commentare.
    if esito["lotti_riusciti"] == 0:
        esito["ok"] = False
        esito["errore"] = ("Nessuna delle %d ricerche è andata a buon fine. %s"
                           % (esito["chiamate"], esito["errore"] or ""))
    elif esito["lotti_falliti"]:
        esito["parziale"] = True
    return esito


def _salva(db, p: dict, per_url: dict, esito: dict) -> bool:
    """Scrive un post se non c'è già. Restituisce True se è nuovo e utile."""
    urn = p.get("id") or p.get("entityId") or p.get("shareUrn")
    if not urn:
        return False

    autore = p.get("author") or {}
    testo = (p.get("content") or p.get("text") or "").strip()
    quando = _quando(p.get("postedAt") or p.get("postedDate"))
    motivo = _motivo_scarto(p, testo, quando)

    # Si risale al candidato dall'indirizzo dell'autore: è quello che permette
    # di mostrare a Sabia con chi sta parlando, non solo che cosa ha scritto.
    url_autore = (autore.get("linkedinUrl") or "").rstrip("/").lower()
    cand = per_url.get(url_autore)
    if not cand and autore.get("publicIdentifier"):
        atteso = f"https://www.linkedin.com/in/{autore['publicIdentifier']}".lower()
        cand = per_url.get(atteso)

    reazioni, commenti = _conta(p, "like"), _conta(p, "comment")
    foto = autore.get("avatar")
    if isinstance(foto, dict):
        foto = foto.get("url")
    elif isinstance(foto, list) and foto:
        foto = (foto[0] or {}).get("url") if isinstance(foto[0], dict) else foto[0]
    foto = foto if isinstance(foto, str) else None

    # Un post già visto non si reinserisce, ma reazioni e commenti cambiano di
    # ora in ora: si aggiornano, altrimenti il rateo che decide l'ordine resta
    # fermo alla prima volta che l'abbiamo guardato.
    if db.execute("SELECT 1 FROM linkedin_post WHERE urn = ?", (urn,)).fetchone():
        db.execute("UPDATE linkedin_post SET reazioni = ?, commenti = ?, punteggio = ?,"
                   " autore_foto = COALESCE(?, autore_foto) WHERE urn = ?",
                   (reazioni, commenti, _punteggio(reazioni, commenti), foto, urn))
        return False

    db.execute(
        "INSERT INTO linkedin_post (urn, candidato_id, autore_nome, autore_url, autore_foto,"
        " testo, url, pubblicato_il, reazioni, commenti, scartato, motivo_scarto, punteggio)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (urn, cand["id"] if cand else None,
         autore.get("name") or f"{autore.get('firstName','')} {autore.get('lastName','')}".strip(),
         autore.get("linkedinUrl"), foto,
         testo, p.get("linkedinUrl") or p.get("url"),
         quando, reazioni, commenti, bool(motivo), motivo,
         _punteggio(reazioni, commenti)))

    if motivo:
        esito["scartati"] += 1
        return False
    return True


# ──────────────────────────────────────────────────────────────────────────
#  La proposta di commento
# ──────────────────────────────────────────────────────────────────────────

# Frasi che non devono finire sotto il post di un consulente: promettono o
# reclutano in modo esplicito, e lì dentro bruciano la credibilità di chi scrive.
_VIETATE = re.compile(
    r"\b(ti (va|andrebbe)|parliamone in privato|ti scrivo in privato|mandami il (cv|curriculum)"
    r"|opportunit[àa] di lavoro|stiamo assumendo|cerchiamo (consulenti|figure|profili)"
    r"|entra (in|nel) (team|squadra)|unisciti a)\b", re.I)

# Un commento che cita cifre che nessuno ha verificato è il modo più rapido di
# farsi correggere in pubblico.
# Il confine di parola dopo «%» pretendeva una lettera subito dopo, quindi
# «rende il 10% annuo» e «siamo al 10%.» passavano indisturbati — cioè proprio
# la forma in cui un consulente promette un rendimento.
_CIFRE = re.compile(
    r"\b\d+([.,]\d+)?\s*(%|percento|per cento|punti base"
    r"|milion[ei]|miliard[oi]|mila|euro|€)(?![a-z])", re.I)

# Riempitivi: occupano battute senza dire niente, e si sentono subito.
_RIEMPITIVI = re.compile(
    r"\b(mi capita spesso|capita spesso di|nel nostro lavoro|nel mio lavoro|"
    r"la verità è che|il punto è che|quello che conta (davvero|è)|"
    r"spesso si dimentica|non tutti sanno che|in questi anni ho imparato|"
    r"aggiungo (solo )?che|mi permetto di)\b", re.I)

# Il gergo di LinkedIn: ogni parola di questa lista fa sembrare il commento
# scritto da chiunque altro.
_GERGO = re.compile(
    r"\b(valore aggiunto|mindset|percorso di crescita|crescita personale|"
    r"fare la differenza|mettersi in gioco|uscire dalla (propria )?comfort zone|"
    r"sinergi[ae]|proattiv[oi]|visione d'insieme|punto di vista interessante|"
    r"riflessione (importante|preziosa)|spunto (di riflessione|interessante))\b", re.I)

# Le aperture retoriche costruite: «Chi pensa che…», «C'è chi crede…».
# Suonano da articolo, non da collega che risponde.
_APERTURA_RETORICA = re.compile(r"^\s*(chi (vede|pensa|crede|dice|immagina)|c'è chi|"
                                r"spesso|troppo spesso|quante volte)\b", re.I)


def _contesto(post: dict) -> str:
    """Le istruzioni per chi scrive il commento.

    Tutto quello che serve sta qui dentro: chi parla, a chi, che cosa ha
    scritto l'altro e cosa non si deve dire. Il modello non deve indovinare
    niente sul contesto.
    """
    chi = post.get("autore_nome") or "un consulente"
    dove = post.get("azienda") or ""
    ruolo = post.get("ruolo_attuale") or ""
    identita = ", ".join(x for x in (ruolo, dove) if x)

    return f"""Scrivi un commento da pubblicare sotto un post di LinkedIn.

CHI COMMENTA
Salvatore Sabia, consulente finanziario di Banca Fideuram a Roma. Guida una
squadra e ne sta cercando altri come lui, ma qui non deve reclutare nessuno:
deve solo dire una cosa sensata, da collega a collega.

SOTTO IL POST DI
{chi}{(' — ' + identita) if identita else ''}

IL POST DICE
\"\"\"{(post.get('testo') or '')[:1200]}\"\"\"

COSA DEVE FARE IL COMMENTO
Una cosa sola, scelta fra queste tre:
  a) un dettaglio concreto del mestiere che conferma o complica quello che dice;
  b) una domanda vera, non retorica, su un punto preciso del post;
  c) un\'esperienza in prima persona, raccontata in una riga.
Chi legge deve capire in due secondi che chi scrive fa questo lavoro davvero.

REGOLE
- UNA frase, al massimo due. Mai più di {MAX_COMMENTO} battute in tutto:
  è la regola più importante, un commento lungo non lo legge nessuno.
- Si parte subito dalla cosa da dire. Niente preamboli, niente «chi pensa che…»,
  niente «spesso si dimentica…».
- Prima persona, tono di un collega che parla alla pari. Mai del lei.
- Niente complimenti generici: «bel post», «ottima riflessione», «grazie per
  la condivisione» e simili sono vietati.
- Niente riempitivi: «mi capita spesso», «nel nostro lavoro», «la verità è che».
- Niente gergo da LinkedIn: «valore aggiunto», «mindset», «spunto di riflessione»,
  «mettersi in gioco».
- Niente cifre, percentuali o dati: non ne abbiamo di verificati per questo post.
- Non proporre lavoro, non invitare in privato, non nominare Fideuram.
- Niente trattino lungo, niente emoji, niente hashtag.
- Italiano parlato, non da comunicato.
- Scrivi solo il commento, senza virgolette e senza spiegazioni."""


def _controlla(testo: str) -> list:
    """I guardrail sul testo generato. Restituisce l'elenco dei problemi."""
    problemi = []
    t = (testo or "").strip()
    if not t:
        return ["il testo è vuoto"]
    if len(t) > MAX_COMMENTO:
        problemi.append(f"troppo lungo: {len(t)} battute invece di {MAX_COMMENTO}")
    frasi = [f for f in re.split(r"(?<=[.!?])\s+", t) if f.strip()]
    if len(frasi) > MAX_FRASI:
        problemi.append(f"{len(frasi)} frasi invece di {MAX_FRASI}")
    if _RIEMPITIVI.search(t):
        problemi.append("contiene un riempitivo che non dice niente")
    if _GERGO.search(t):
        problemi.append("usa il gergo di LinkedIn")
    if _APERTURA_RETORICA.search(t):
        problemi.append("apre con una costruzione retorica invece che con la cosa da dire")
    if _VIETATE.search(t):
        problemi.append("propone lavoro o sposta in privato: sotto un post pubblico non si fa")
    if _CIFRE.search(t):
        problemi.append("cita cifre che non abbiamo verificato")
    if re.search(r"\b(bel post|ottim[ao] (post|riflessione|spunto)|grazie per (la|il)|"
                 r"condivido in pieno|assolutamente d'accordo)\b", t, re.I):
        problemi.append("è un complimento generico")
    if "—" in t:
        problemi.append("contiene il trattino lungo")
    return problemi


def _scrivi_con_argon(prompt: str):
    """Il copywriter di Axiom se c'è, Claude come riserva dichiarata."""
    percorso = os.path.expanduser("~/scripts/axiom")
    if os.path.isdir(percorso):
        if percorso not in sys.path:
            sys.path.insert(0, percorso)
        try:
            from copywriter import scrivi
            return scrivi(prompt, tipo="messaggio")
        except Exception as e:
            log.warning("Argon non disponibile (%s): scrive Claude", e)
    from ai_helpers import client, CLAUDE_MODEL
    r = client.messages.create(model=CLAUDE_MODEL, max_tokens=400,
                               messages=[{"role": "user", "content": prompt}])
    return r.content[0].text.strip(), "claude"


def genera_proposta(urn: str, riprova: bool = True) -> dict:
    """Scrive la proposta di commento per un post e la salva.

    Se il testo non supera i controlli si riprova una volta sola, spiegando
    l'errore a chi scrive. Al secondo fallimento la proposta viene salvata lo
    stesso ma segnalata: meglio che Sabia veda un testo imperfetto e lo corregga,
    piuttosto che la riga sparisca senza che nessuno sappia perché.
    """
    db = get_db()
    try:
        post = db.execute(
            "SELECT p.*, c.azienda, c.ruolo_attuale FROM linkedin_post p"
            " LEFT JOIN candidati c ON c.id = p.candidato_id WHERE p.urn = ?", (urn,)).fetchone()
        if not post:
            return {"ok": False, "errore": "Post non trovato."}
        post = dict(post)

        prompt = _contesto(post)
        testo, autore = _scrivi_con_argon(prompt)
        problemi = _controlla(testo)

        if problemi and riprova:
            log.info("Commento rifatto per %s: %s", urn, "; ".join(problemi))
            testo, autore = _scrivi_con_argon(
                prompt + "\n\nIL TENTATIVO PRECEDENTE È STATO SCARTATO PERCHÉ: "
                + "; ".join(problemi) + ".\nRiscrivilo evitando quell'errore.")
            problemi = _controlla(testo)

        db.execute("DELETE FROM linkedin_commenti WHERE post_urn = ? AND stato = 'proposto'", (urn,))
        cur = db.execute(
            "INSERT INTO linkedin_commenti (post_urn, testo_proposto, autore_testo, angolo)"
            " VALUES (?,?,?,?)",
            (urn, testo.strip(), autore, "; ".join(problemi) if problemi else None))
        db.commit()
        # L'identificativo serve alla pagina per disegnare la proposta al posto
        # giusto senza ricaricare tutto.
        return {"ok": True, "testo": testo.strip(), "autore": autore,
                "problemi": problemi, "commento_id": cur.lastrowid, "urn": urn}
    finally:
        db.close()


# ──────────────────────────────────────────────────────────────────────────
#  Quello che vede Sabia
# ──────────────────────────────────────────────────────────────────────────

# Le due condizioni che fanno decadere un post dalla lista, usate sia quando si
# legge sia quando si conta: devono restare identiche, altrimenti il numero in
# alto e le righe sotto raccontano due storie diverse.
_FRESCHI = ("   AND (p.pubblicato_il IS NULL OR p.pubblicato_il > ?)"
            "   AND (c.id IS NULL OR c.stato <> 'Chiuso')")


def da_approvare(limite: int = 20) -> list:
    """I post buoni, dal più conveniente, con la proposta se già c'è."""
    db = get_db()
    try:
        righe = db.execute(
            "SELECT p.urn, p.autore_nome, p.autore_url, p.autore_foto, p.testo, p.url, p.pubblicato_il,"
            " p.reazioni, p.commenti, p.punteggio,"
            " c.id AS candidato_id, c.nome, c.cognome, c.azienda, c.ruolo_attuale,"
            " c.punteggio AS punteggio_candidato, c.stato,"
            " k.id AS commento_id, k.testo_proposto, k.testo_finale, k.stato AS stato_commento,"
            " k.autore_testo, k.angolo"
            " FROM linkedin_post p"
            " LEFT JOIN candidati c ON c.id = p.candidato_id"
            " LEFT JOIN linkedin_commenti k ON k.post_urn = p.urn AND k.stato <> 'scartato'"
            " WHERE p.scartato = FALSE"
            "   AND (k.stato IS NULL OR k.stato IN ('proposto', 'approvato'))"
            # Un post invecchia anche dopo essere entrato in lista, e la persona
            # può essere stata chiusa nel frattempo: vedi _FRESCHI.
            + _FRESCHI +
            # In PostgreSQL DESC mette i NULL per primi: senza COALESCE i post
            # senza proposta scavalcherebbero quelli già approvati, che con più
            # di venti righe finirebbero fuori dalla pagina.
            " ORDER BY COALESCE(k.stato = 'approvato', FALSE) DESC,"
            "          p.punteggio DESC, p.pubblicato_il DESC NULLS LAST"
            " LIMIT ?", (datetime.now() - timedelta(days=GIORNI_UTILI), limite)).fetchall()
        return [dict(r) for r in righe]
    finally:
        db.close()


def decidi(commento_id: int, azione: str, testo: str = None) -> dict:
    """Approva, scarta o segna come pubblicato una proposta.

    Il testo che Sabia approva viene salvato a parte: la differenza con quello
    proposto è l'unico materiale onesto per capire come correggere il modo di
    scrivere.
    """
    if azione not in ("approva", "scarta", "pubblicato"):
        return {"ok": False, "errore": "Azione non riconosciuta."}
    nuovo = {"approva": "approvato", "scarta": "scartato", "pubblicato": "pubblicato"}[azione]

    pulito = (testo or "").strip() or None

    # Se il campo è stato svuotato di proposito, COALESCE terrebbe buono il testo
    # di prima e si approverebbe qualcosa che Sabia ha cancellato. Meglio
    # fermarsi: un commento vuoto non si pubblica, e la cancellazione è un modo
    # di dire «questo non va», non «va bene così».
    if azione == "approva" and testo is not None and not pulito:
        return {"ok": False,
                "errore": "Il commento è vuoto: scrivi il testo o usa «Lascia perdere»."}

    db = get_db()
    try:
        if azione == "pubblicato":
            # Il testo resta modificabile anche dopo l'approvazione: va salvato
            # quello davvero copiato, altrimenti in archivio resta una versione
            # diversa da quella finita sotto il post — e la misura di quanto
            # scriviamo bene si baserebbe su un testo mai pubblicato.
            db.execute("UPDATE linkedin_commenti SET stato = ?, pubblicato_il = CURRENT_TIMESTAMP,"
                       " testo_finale = COALESCE(?, testo_finale) WHERE id = ?",
                       (nuovo, pulito, commento_id))
        else:
            db.execute("UPDATE linkedin_commenti SET stato = ?, deciso_il = CURRENT_TIMESTAMP,"
                       " testo_finale = COALESCE(?, testo_finale) WHERE id = ?",
                       (nuovo, pulito, commento_id))

        if azione == "scarta":
            # «Lascia perdere» vuol dire «su questo post non commento», non
            # «riscrivimelo»: senza chiudere anche il post, il giro dopo
            # tornerebbe in lista come se non avesse mai avuto una proposta.
            riga = db.execute("SELECT post_urn FROM linkedin_commenti WHERE id = ?",
                              (commento_id,)).fetchone()
            if riga:
                db.execute("UPDATE linkedin_post SET scartato = TRUE,"
                           " motivo_scarto = COALESCE(motivo_scarto, ?) WHERE urn = ?",
                           ("lasciato perdere a mano", riga["post_urn"]))
        db.commit()
        return {"ok": True, "stato": nuovo}
    finally:
        db.close()


def riepilogo() -> dict:
    """I numeri della sezione, compreso quanto spesso il testo proposto va bene
    così com'è: è la misura che dice se stiamo imparando a scrivere come lui."""
    db = get_db()
    try:
        soglia = datetime.now() - timedelta(days=GIORNI_UTILI)

        def uno(sql, par=()):
            r = db.execute(sql, par).fetchone()
            return (r["n"] if r and r["n"] is not None else 0)

        decisi = db.execute(
            "SELECT testo_proposto, testo_finale FROM linkedin_commenti"
            " WHERE stato IN ('approvato','pubblicato')").fetchall()
        intatti = sum(1 for r in decisi
                      if not r["testo_finale"]
                      or r["testo_finale"].strip() == r["testo_proposto"].strip())

        return {
            "profili_seguiti": len(profili_da_seguire()),
            "post_utili": uno("SELECT COUNT(*) n FROM linkedin_post WHERE scartato = FALSE"),
            "post_scartati": uno("SELECT COUNT(*) n FROM linkedin_post WHERE scartato = TRUE"),
            # Quanti post aspettano una decisione: sia quelli con una proposta già
            # scritta, sia quelli ancora senza. Contare solo le proposte scritte
            # direbbe «1 da leggere» con diciotto post fermi in lista.
            # Gli stessi filtri della lista: un contatore che promette venti
            # post da leggere mentre la pagina ne mostra tre è peggio di nessun
            # contatore.
            "da_decidere": uno(
                "SELECT COUNT(*) n FROM linkedin_post p"
                " LEFT JOIN candidati c ON c.id = p.candidato_id"
                " LEFT JOIN linkedin_commenti k ON k.post_urn = p.urn AND k.stato <> 'scartato'"
                " WHERE p.scartato = FALSE AND (k.stato IS NULL OR k.stato = 'proposto')"
                + _FRESCHI, (soglia,)),
            "senza_proposta": uno(
                "SELECT COUNT(*) n FROM linkedin_post p"
                " LEFT JOIN candidati c ON c.id = p.candidato_id"
                " LEFT JOIN linkedin_commenti k ON k.post_urn = p.urn AND k.stato <> 'scartato'"
                " WHERE p.scartato = FALSE AND k.id IS NULL"
                + _FRESCHI, (soglia,)),
            "approvati": uno("SELECT COUNT(*) n FROM linkedin_commenti WHERE stato = 'approvato'"),
            "pubblicati": uno("SELECT COUNT(*) n FROM linkedin_commenti WHERE stato = 'pubblicato'"),
            "decisi": len(decisi),
            "intatti": intatti,
            "quota_intatti": round(intatti / len(decisi) * 100) if decisi else None,
        }
    finally:
        db.close()
