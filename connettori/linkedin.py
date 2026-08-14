"""
Connettore LinkedIn — adapter NON invasivo sul codice esistente in routes/ricerca.py.

Non riscrive nulla: riusa `cerca_apify` e `normalizza_profilo` già collaudati e li
espone dietro il contratto comune (connettori/base.py). È il primo passo della
rifattorizzazione (piano §6 Fase 0): la logica resta in ricerca.py finché non la
spostiamo qui, ma tutto il nuovo codice multi-fonte passa già da questa interfaccia.

L'import è lazy dentro le funzioni per evitare import circolari (routes/ricerca.py
importa molte dipendenze dell'app).
"""

from connettori.base import profilo_normalizzato

FONTE = "linkedin"


def cerca(parametri: dict, progress_cb=None):
    """
    Ingestione di volume da LinkedIn via Apify. Delega a routes.ricerca.cerca_apify.
    Ritorna (lista_grezzi, errore) — stessa firma dell'originale.
    """
    from routes.ricerca import cerca_apify
    return cerca_apify(
        ruolo=parametri.get("ruolo", ""),
        citta=parametri.get("citta", ""),
        paese=parametri.get("paese", ""),
        azienda=parametri.get("azienda", ""),
        parole_chiave=parametri.get("parole_chiave", ""),
        num_pagine=parametri.get("num_pagine", 1),
        progress_cb=progress_cb,
    )


def normalizza(profilo_grezzo: dict) -> dict:
    """Mappa l'output di ricerca.normalizza_profilo nello schema candidato standard."""
    from routes.ricerca import normalizza_profilo
    p = normalizza_profilo(profilo_grezzo)
    return profilo_normalizzato(
        nome=p.get("nome", ""),
        cognome=p.get("cognome", ""),
        ruolo_attuale=p.get("ruolo", ""),
        azienda=p.get("azienda", ""),
        location=p.get("location", ""),
        url_fonte=p.get("linkedin", ""),
        testo_profilo=p.get("sommario", ""),
        source=FONTE,
    )
