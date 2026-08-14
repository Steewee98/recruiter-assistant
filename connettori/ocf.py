"""
Connettore OCF — Organismo di vigilanza e tenuta dell'Albo unico dei Consulenti Finanziari.

Serve alla TRIANGOLAZIONE: dato un nominativo, verifica se è iscritto all'albo,
in quale sezione e (dove disponibile) con quale mandante/rete opera oggi.
Il registro è pubblico: organismocf.it → "Ricerca nelle sezioni dell'albo"
(dati ex art. 146 Reg. Intermediari Consob 20307/18).

⚠️ STATO: la struttura esatta dell'endpoint di ricerca del portale OCF va confermata
con lo spike tecnico (piano §6 Fase 3, 1 giorno). Finché non è confermata, questo
connettore fa un tentativo best-effort e, se non riesce a interpretare la risposta,
ritorna RisultatoFonte(ok=False) SENZA mai sollevare — così la triangolazione degrada
con grazia usando le altre fonti.

Per attivare il tentativo di rete impostare la variabile d'ambiente OCF_LOOKUP_ATTIVO=1
(di default è disattivato per non fare chiamate esterne non verificate).
"""

import os
import logging

from connettori.base import RisultatoFonte

logger = logging.getLogger(__name__)

FONTE = "ocf"
OCF_BASE = "https://www.organismocf.it"
TIMEOUT = 15


def cerca_per_nome(nome: str, cognome: str, contesto: dict = None, forza: bool = False) -> RisultatoFonte:
    """
    Verifica l'iscrizione all'albo OCF di un nominativo.
    Non solleva mai: in caso di dubbio ritorna ok=False con una nota diagnostica.
    `forza=True` attiva il lookup anche senza la env var (scelta esplicita del chiamante).
    """
    r = RisultatoFonte(fonte=FONTE)
    nome = (nome or "").strip()
    cognome = (cognome or "").strip()
    if not cognome:
        r.errore = "Cognome mancante: impossibile interrogare l'albo OCF."
        return r

    # Guardrail: chiamata esterna disattivata finché l'endpoint non è confermato dallo spike.
    if not forza and os.environ.get("OCF_LOOKUP_ATTIVO") != "1":
        r.errore = ("Lookup OCF disattivato (OCF_LOOKUP_ATTIVO!=1). "
                    "Endpoint del portale da confermare con lo spike tecnico.")
        r.note = "scaffold_pronto"
        return r

    try:
        import requests
        # NB: parametri e path da confermare con lo spike. Tentativo best-effort.
        resp = requests.get(
            f"{OCF_BASE}/portal/web/portale-ocf/ricerca-nelle-sezioni-dell-albo",
            params={"cognome": cognome, "nome": nome},
            timeout=TIMEOUT,
            headers={"User-Agent": "SABIA-Recruiting/1.0 (verifica albo)"},
        )
        r.ok = resp.status_code == 200
        if not r.ok:
            r.errore = f"OCF ha risposto {resp.status_code}."
            return r
        # La pagina è probabilmente renderizzata lato server con una tabella risultati.
        # Il parsing preciso va tarato sullo spike; qui rileviamo solo presenza grezza.
        testo = resp.text.lower()
        if cognome.lower() in testo:
            r.trovato = True
            r.iscritto_ocf = True
            r.note = "Match grezzo sul cognome nella pagina — parsing dettagliato da rifinire con lo spike."
        else:
            r.iscritto_ocf = False
            r.note = "Nessun match evidente nella pagina."
        return r
    except Exception as e:  # robustezza a cascata: nessun errore fatale
        logger.warning("[OCF] lookup fallito per %s %s: %s", nome, cognome, e)
        r.errore = f"Lookup OCF non riuscito: {type(e).__name__}."
        return r
