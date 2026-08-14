"""
Package connettori — una fonte di reclutamento per modulo, tutte dietro lo stesso
contratto (base.py). Il dispatcher instrada per nome fonte, così un solo punto del
core serve LinkedIn, OCF, job board e fonti di riconoscimento.

Vedi docs/piano_multifonte.md §2.
"""

from connettori import linkedin, ocf, riconoscimenti

# Registry nome-fonte → modulo connettore
CONNETTORI = {
    linkedin.FONTE: linkedin,
    ocf.FONTE: ocf,
    riconoscimenti.FONTE: riconoscimenti,
}


def get_connettore(fonte: str):
    """Restituisce il modulo connettore per la fonte richiesta (o KeyError)."""
    return CONNETTORI[fonte]


def fonti_registrate() -> list:
    """Elenco delle fonti attualmente disponibili."""
    return list(CONNETTORI.keys())
