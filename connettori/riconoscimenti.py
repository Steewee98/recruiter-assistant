"""
Connettore Fonti di Riconoscimento — classifiche, premi e testate di settore.

Serve alla TRIANGOLAZIONE: dato un nominativo, cerca se il consulente è già stato
premiato o inserito in classifiche (segnale forte di qualità già validato da terzi),
per capire se "ci piace più di quanto dica LinkedIn da solo".

Fonti mappate (ricerca 08/2026, vedi piano §5.3):
  - Bluerating  — Bluerating Awards / Private Banking Awards / pagelle reti
  - Citywire IT — Wealth Awards + Top 50 consulenti
  - Forbes IT   — Private Banking Awards

⚠️ STATO: le classifiche vivono dentro articoli non strutturati. Il match affidabile
per nominativo richiede o una search API o l'estrazione AII dagli articoli (piano §5.3).
Questo connettore espone l'interfaccia e le fonti; il lookup di rete è disattivato di
default (RICONOSCIMENTI_LOOKUP_ATTIVO=1 per abilitarlo). Non solleva mai.
"""

import os
import logging

from connettori.base import RisultatoFonte

logger = logging.getLogger(__name__)

FONTE = "riconoscimenti"
TIMEOUT = 15

# Catalogo fonti: nome mostrato + dominio + pattern URL di ricerca (da rifinire).
FONTI_RICONOSCIMENTO = [
    {"nome": "Bluerating", "dominio": "bluerating.com",
     "ricerca": "https://www.bluerating.com/?s={q}"},
    {"nome": "Citywire Italia", "dominio": "citywire.com",
     "ricerca": "https://citywire.com/it/search?q={q}"},
    {"nome": "Forbes Italia", "dominio": "forbes.it",
     "ricerca": "https://forbes.it/?s={q}"},
]


def fonti_disponibili() -> list:
    """Elenco leggibile delle fonti di riconoscimento coperte (per UI/diagnostica)."""
    return [f["nome"] for f in FONTI_RICONOSCIMENTO]


def cerca_per_nome(nome: str, cognome: str, contesto: dict = None, forza: bool = False) -> RisultatoFonte:
    """
    Cerca premi/menzioni del nominativo nelle testate di settore.
    Non solleva mai: ritorna RisultatoFonte con la lista (eventuale) di riconoscimenti.
    `forza=True` attiva il lookup anche senza la env var (scelta esplicita del chiamante).
    """
    r = RisultatoFonte(fonte=FONTE)
    nome = (nome or "").strip()
    cognome = (cognome or "").strip()
    if not cognome:
        r.errore = "Cognome mancante: impossibile cercare riconoscimenti."
        return r

    if not forza and os.environ.get("RICONOSCIMENTI_LOOKUP_ATTIVO") != "1":
        r.errore = ("Lookup riconoscimenti disattivato (RICONOSCIMENTI_LOOKUP_ATTIVO!=1). "
                    "Estrazione dagli articoli da implementare (piano §5.3).")
        r.note = f"fonti_pronte: {', '.join(fonti_disponibili())}"
        return r

    # Best-effort: interroga la ricerca interna di ogni testata e rileva presenza grezza.
    # Il match preciso (è davvero un premio? quale anno?) va delegato all'estrazione AI:
    # qui raccogliamo solo i candidati-URL da passare eventualmente al layer AI.
    try:
        import requests
        query = f"{nome} {cognome}".strip().replace(" ", "+")
        for f in FONTI_RICONOSCIMENTO:
            try:
                url = f["ricerca"].format(q=query)
                resp = requests.get(url, timeout=TIMEOUT,
                                    headers={"User-Agent": "SABIA-Recruiting/1.0"})
                if resp.status_code == 200 and cognome.lower() in resp.text.lower():
                    from connettori.base import RiconoscimentoTrovato
                    r.riconoscimenti.append(RiconoscimentoTrovato(
                        fonte=f["nome"],
                        titolo=f"Possibile menzione su {f['nome']} (da verificare)",
                        url=url,
                    ))
            except Exception as e:
                logger.debug("[Riconoscimenti] %s ko: %s", f["nome"], e)
                continue
        r.ok = True
        r.trovato = len(r.riconoscimenti) > 0
        if not r.trovato:
            r.note = "Nessuna menzione evidente nelle testate coperte."
        return r
    except Exception as e:
        logger.warning("[Riconoscimenti] lookup fallito: %s", e)
        r.errore = f"Lookup riconoscimenti non riuscito: {type(e).__name__}."
        return r
