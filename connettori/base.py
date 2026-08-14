"""
Contratto comune a tutte le fonti di reclutamento (LinkedIn, OCF, job board, riconoscimenti).

Ogni connettore in questo package espone la stessa interfaccia, così il core
dell'app (filtri → valutazione AI → pipeline → recap) resta identico per qualsiasi fonte.
È il pattern descritto in docs/piano_multifonte.md §2.

Un connettore fornisce due capacità (non deve implementarle tutte):
  - cerca(parametri, progress_cb=None) -> (lista_profili_grezzi, errore)   # ingestione di volume
  - cerca_per_nome(nome, cognome, contesto=None) -> RisultatoFonte          # lookup per triangolazione
  - normalizza(profilo_grezzo) -> dict                                       # → schema candidato standard

Design MIT/Axiom:
  - Contratto esplicito e uniforme (nessun caller deve sapere da che fonte arriva il dato).
  - Robustezza a cascata: i lookup non sollevano mai; ritornano un RisultatoFonte con `ok=False`.
"""

from dataclasses import dataclass, field, asdict
from typing import Optional


# Valori ammessi per il campo candidati.source
FONTI = ("linkedin", "ocf", "jobboard", "riconoscimenti")


def profilo_normalizzato(
    nome: str = "",
    cognome: str = "",
    ruolo_attuale: str = "",
    azienda: str = "",
    location: str = "",
    url_fonte: str = "",
    testo_profilo: str = "",
    source: str = "",
    extra: dict = None,
) -> dict:
    """
    Costruisce il dict candidato standard. TUTTI i connettori.normalizza()
    devono restituire questa forma, così il core non cambia mai.
    """
    return {
        "nome": (nome or "").strip(),
        "cognome": (cognome or "").strip(),
        "ruolo_attuale": (ruolo_attuale or "").strip(),
        "azienda": (azienda or "").strip(),
        "location": (location or "").strip(),
        "url_fonte": (url_fonte or "").strip(),
        "testo_profilo": (testo_profilo or "").strip(),
        "source": source,
        "extra": extra or {},
    }


def chiave_dedup(profilo: dict) -> str:
    """
    Chiave per deduplica cross-fonte (vedi piano §3).
    Preferisce l'URL fonte (univoco); altrimenti nome+cognome+azienda normalizzati.
    La stessa persona trovata su LinkedIn e su OCF deve produrre la stessa chiave
    quando l'URL manca.
    """
    url = (profilo.get("url_fonte") or profilo.get("profilo_linkedin") or "").strip().lower()
    if url:
        # normalizza l'URL (togli protocollo, slash finale, query)
        url = url.split("?")[0].rstrip("/")
        url = url.replace("https://", "").replace("http://", "").replace("www.", "")
        return "url:" + url
    nome = (profilo.get("nome") or "").strip().lower()
    cognome = (profilo.get("cognome") or "").strip().lower()
    azienda = (profilo.get("azienda") or "").strip().lower()
    return f"nc:{nome}|{cognome}|{azienda}"


@dataclass
class RiconoscimentoTrovato:
    """Un singolo premio/menzione trovato in una fonte editoriale."""
    fonte: str = ""          # es. "Bluerating", "Citywire"
    titolo: str = ""         # es. "Bluerating Awards 2025 — Miglior consulente Fideuram"
    anno: str = ""
    url: str = ""


@dataclass
class RisultatoFonte:
    """
    Esito di un lookup su una fonte per la triangolazione.
    Robustezza a cascata: `ok=False` + `errore` invece di sollevare eccezioni.
    """
    fonte: str
    ok: bool = False
    trovato: bool = False
    errore: str = ""
    # Dati OCF
    iscritto_ocf: Optional[bool] = None
    sezione_albo: str = ""
    mandante_attuale: str = ""      # rete/banca con cui opera oggi
    data_iscrizione: str = ""
    # Dati riconoscimenti
    riconoscimenti: list = field(default_factory=list)   # list[RiconoscimentoTrovato]
    # Note libere / payload grezzo per debug
    note: str = ""

    def to_dict(self) -> dict:
        d = asdict(self)
        return d
