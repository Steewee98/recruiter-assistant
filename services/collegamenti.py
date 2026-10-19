"""Confronta i collegamenti LinkedIn di Sabia con i candidati in «Richiesta Inviata».

Serve a rispondere a una domanda sola: a chi possiamo mandare il messaggio,
perché ha accettato il collegamento.

LinkedIn non espone da nessuna parte l'elenco di chi ha accettato una richiesta.
L'unico modo pulito è l'esportazione ufficiale dei propri collegamenti:

    LinkedIn → Impostazioni e privacy → Privacy dei dati
    → Ottieni una copia dei tuoi dati → «Connessioni» → Richiedi archivio

Arriva per email un file Connections.csv con nome, cognome, URL del profilo,
azienda, ruolo e la data in cui il collegamento è avvenuto.

Uso:
    venv/bin/python services/collegamenti.py ~/Downloads/Connections.csv
    venv/bin/python services/collegamenti.py ~/Downloads/Connections.csv --csv esito.csv

Lo strumento NON scrive nulla sul database: produce solo il quadro. Lo stato dei
candidati si cambia dalla pipeline, a ragion veduta.
"""

import csv
import io
import re
import sys
import unicodedata
from datetime import datetime

STATO_RICHIESTA = "Richiesta Inviata"

# Gli URL del tipo linkedin.com/in/ACwAAB... sono identificativi interni che
# LinkedIn restituisce allo scraper: nell'esportazione dei collegamenti non
# compaiono mai. Per quei candidati l'unico aggancio possibile è il nome.
PREFISSO_OPACO = "acwa"


def _senza_accenti(testo: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", testo)
                   if unicodedata.category(c) != "Mn")


def _normalizza(testo) -> str:
    """Minuscole, senza accenti, senza punteggiatura, spazi singoli."""
    if not testo:
        return ""
    t = _senza_accenti(str(testo)).lower()
    t = re.sub(r"[^\w\s]", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def _slug(url) -> str:
    """Estrae la parte identificativa di un URL LinkedIn: /in/<slug>."""
    if not url:
        return ""
    m = re.search(r"/in/([^/?#]+)", str(url).strip().rstrip("/"), re.I)
    if not m:
        return ""
    return _senza_accenti(m.group(1)).lower()


def _chiavi_nome(nome, cognome) -> set:
    """Le due letture possibili di un nome.

    Nell'archivio capita che nome e cognome siano invertiti («Gargiulo
    Alessandro» con nome=Gargiulo): si tengono buone entrambe le letture,
    e il confronto vale se una qualsiasi combacia.
    """
    a, b = _normalizza(nome), _normalizza(cognome)
    if not a and not b:
        return set()
    return {k for k in (f"{a} {b}".strip(), f"{b} {a}".strip()) if k}


def leggi_export(percorso: str) -> list:
    """Legge il Connections.csv di LinkedIn.

    Il file comincia con alcune righe di avviso prima dell'intestazione vera:
    si cerca la riga che contiene «First Name» e si parte da lì.
    """
    with open(percorso, "r", encoding="utf-8-sig", errors="replace") as f:
        righe = f.read().splitlines()

    inizio = next((i for i, r in enumerate(righe) if "First Name" in r), None)
    if inizio is None:
        raise ValueError(
            "Non sembra l'esportazione dei collegamenti: manca la colonna «First Name». "
            "Controlla di aver scaricato «Connessioni» e non l'archivio completo.")

    lettore = csv.DictReader(io.StringIO("\n".join(righe[inizio:])))
    fuori = []
    for r in lettore:
        nome = (r.get("First Name") or "").strip()
        cognome = (r.get("Last Name") or "").strip()
        if not nome and not cognome:
            continue
        fuori.append({
            "nome": nome,
            "cognome": cognome,
            "url": (r.get("URL") or "").strip(),
            "azienda": (r.get("Company") or "").strip(),
            "ruolo": (r.get("Position") or "").strip(),
            "il": (r.get("Connected On") or "").strip(),
        })
    return fuori


def _data(testo: str):
    """«02 Oct 2026» → date. Restituisce None se il formato cambia."""
    for fmt in ("%d %b %Y", "%d %B %Y", "%Y-%m-%d", "%d/%m/%Y"):
        try:
            return datetime.strptime(testo, fmt).date()
        except (ValueError, TypeError):
            continue
    return None


def confronta(collegamenti: list, candidati: list) -> dict:
    """Incrocia le due liste.

    Due agganci, in ordine di affidabilità:
      1. lo slug dell'URL — certo, è lo stesso profilo;
      2. nome e cognome normalizzati — buono, ma se nei collegamenti ci sono
         due persone con lo stesso nome non si assegna: finisce fra i dubbi.
    """
    per_slug = {}
    per_nome = {}
    for c in collegamenti:
        s = _slug(c["url"])
        if s:
            per_slug.setdefault(s, []).append(c)
        for k in _chiavi_nome(c["nome"], c["cognome"]):
            per_nome.setdefault(k, []).append(c)

    accettati, in_attesa, dubbi = [], [], []

    for cand in candidati:
        slug = _slug(cand["profilo_linkedin"])
        opaco = slug.startswith(PREFISSO_OPACO)

        if slug and not opaco and slug in per_slug:
            accettati.append({**cand, "collegamento": per_slug[slug][0], "come": "url"})
            continue

        trovati = []
        for k in _chiavi_nome(cand["nome"], cand["cognome"]):
            for c in per_nome.get(k, []):
                if c not in trovati:
                    trovati.append(c)

        if len(trovati) == 1:
            accettati.append({**cand, "collegamento": trovati[0], "come": "nome"})
        elif len(trovati) > 1:
            dubbi.append({**cand, "candidati_collegamento": trovati,
                          "perche": f"{len(trovati)} collegamenti con lo stesso nome"})
        else:
            in_attesa.append({**cand, "solo_nome": opaco})

    return {"accettati": accettati, "in_attesa": in_attesa, "dubbi": dubbi,
            "totale_collegamenti": len(collegamenti), "totale_candidati": len(candidati)}


def _candidati_dal_db():
    from database import get_db
    db = get_db()
    try:
        righe = db.execute(
            "SELECT id, nome, cognome, azienda, ruolo_attuale, profilo_linkedin, punteggio,"
            " data_aggiornamento FROM candidati WHERE stato = ? ORDER BY data_aggiornamento DESC",
            (STATO_RICHIESTA,)).fetchall()
        return [dict(r) for r in righe]
    finally:
        db.close()


def _giorni(cand, coll):
    """Giorni fra l'invio della richiesta e l'accettazione.

    La data di invio arriva dal database come testo o come datetime a seconda
    del driver: si normalizzano entrambe le forme prima di sottrarle.
    """
    inviata = cand.get("data_aggiornamento")
    accettata = _data(coll.get("il", ""))
    if not inviata or not accettata:
        return None
    if isinstance(inviata, str):
        inviata = _data(inviata[:10])
    elif hasattr(inviata, "date"):
        inviata = inviata.date()
    if not inviata:
        return None
    return (accettata - inviata).days


def stampa(esito: dict) -> None:
    acc, att, dub = esito["accettati"], esito["in_attesa"], esito["dubbi"]
    tot = esito["totale_candidati"]
    quota = f"{len(acc) / tot * 100:.0f}%" if tot else "—"

    print(f"\nCollegamenti nell'esportazione: {esito['totale_collegamenti']}")
    print(f"Candidati in «{STATO_RICHIESTA}»: {tot}\n")
    print(f"  HANNO ACCETTATO   {len(acc):>3}   ({quota} delle richieste)")
    print(f"  ancora in attesa  {len(att):>3}")
    print(f"  da controllare    {len(dub):>3}")

    if acc:
        print(f"\n{'─' * 78}\nA QUESTI PUOI SCRIVERE\n{'─' * 78}")
        def ordine(x):
            g = _giorni(x, x["collegamento"])
            return (g is None, g if g is not None else 0)
        for x in sorted(acc, key=ordine):
            g = _giorni(x, x["collegamento"])
            quando = f"accettata in {g} gg" if g is not None and g >= 0 else \
                     (x["collegamento"].get("il") or "data ignota")
            segno = "" if x["come"] == "url" else "  (agganciato per nome)"
            azienda = x["collegamento"].get("azienda") or x.get("azienda") or ""
            print(f"  {x['nome']} {x['cognome']}")
            print(f"      {azienda[:52]:<52} {quando}{segno}")
            print(f"      {x['profilo_linkedin']}")

    if dub:
        print(f"\n{'─' * 78}\nDA CONTROLLARE A MANO\n{'─' * 78}")
        for x in dub:
            print(f"  {x['nome']} {x['cognome']} — {x['perche']}")
            for c in x["candidati_collegamento"]:
                print(f"      · {c['nome']} {c['cognome']} · {c.get('azienda','')} · {c['url']}")

    if att:
        senza_url = [x for x in att if x.get("solo_nome")]
        print(f"\n{'─' * 78}\nNON ANCORA ACCETTATA ({len(att)})\n{'─' * 78}")
        for x in att[:15]:
            nota = "  ·  profilo senza indirizzo leggibile: verificato solo per nome" \
                   if x.get("solo_nome") else ""
            print(f"  {x['nome']} {x['cognome']}{nota}")
        if len(att) > 15:
            print(f"  … e altri {len(att) - 15}")
        if senza_url:
            print(f"\n  Attenzione: {len(senza_url)} di questi hanno un indirizzo LinkedIn non")
            print("  confrontabile, quindi il controllo è avvenuto solo sul nome. Se uno di loro")
            print("  si è collegato usando un nome scritto diversamente, qui risulta in attesa.")


def salva_csv(esito: dict, percorso: str) -> None:
    with open(percorso, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["esito", "nome", "cognome", "azienda", "profilo", "accettata_il",
                    "giorni", "agganciato_per"])
        for x in esito["accettati"]:
            c = x["collegamento"]
            w.writerow(["accettata", x["nome"], x["cognome"],
                        c.get("azienda") or x.get("azienda") or "",
                        x["profilo_linkedin"], c.get("il", ""),
                        _giorni(x, c) if _giorni(x, c) is not None else "", x["come"]])
        for x in esito["dubbi"]:
            w.writerow(["da controllare", x["nome"], x["cognome"], x.get("azienda") or "",
                        x["profilo_linkedin"], "", "", x["perche"]])
        for x in esito["in_attesa"]:
            w.writerow(["in attesa", x["nome"], x["cognome"], x.get("azienda") or "",
                        x["profilo_linkedin"], "", "",
                        "solo nome" if x.get("solo_nome") else ""])
    print(f"\nScritto {percorso}")


def main(argv: list) -> int:
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    percorso = argv[0]
    uscita = None
    if "--csv" in argv:
        i = argv.index("--csv")
        if i + 1 >= len(argv):
            print("Manca il nome del file dopo --csv")
            return 2
        uscita = argv[i + 1]

    try:
        collegamenti = leggi_export(percorso)
    except FileNotFoundError:
        print(f"File non trovato: {percorso}")
        return 2
    except ValueError as e:
        print(str(e))
        return 2

    if not collegamenti:
        print("L'esportazione non contiene collegamenti: controlla il file.")
        return 2

    esito = confronta(collegamenti, _candidati_dal_db())
    stampa(esito)
    if uscita:
        salva_csv(esito, uscita)
    return 0


if __name__ == "__main__":
    import os
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass
    sys.exit(main(sys.argv[1:]))
