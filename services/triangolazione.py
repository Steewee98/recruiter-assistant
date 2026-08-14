"""
Servizio di TRIANGOLAZIONE contatto.

Dato un candidato già in pipeline (arrivato tipicamente da LinkedIn), incrocia:
  - LinkedIn  → dati/valutazione già in nostro possesso
  - OCF       → verifica iscrizione albo + mandante attuale
  - Riconoscimenti → premi/menzioni di settore
e produce un dossier che risponde a: "ci piace più di LinkedIn?" e "come lo contattiamo?".

Metodo Axiom/MIT:
  - Contratto d'agente: input candidato → output dossier con schema fisso e validato.
  - Robustezza a cascata: se una fonte fallisce, la triangolazione continua e lo dichiara.
  - Guardrail: senza cognome non si triangola; l'esito AI è validato prima di restituirlo.
"""

import logging

from connettori.ocf import cerca_per_nome as ocf_lookup
from connettori.riconoscimenti import cerca_per_nome as ric_lookup
from ai_helpers import triangola_profilo, messaggio_errore_ai

logger = logging.getLogger(__name__)

# Chiavi che il dossier AI deve sempre contenere (guardrail di output)
CHIAVI_DOSSIER = {
    "preferenza_vs_linkedin", "priorita", "confidenza_dati",
    "sintesi_triangolazione", "come_contattarlo",
}


def _valida_dossier(d: dict) -> dict:
    """Riempie eventuali chiavi mancanti con default prudenti (non fidarsi ciecamente dell'AI)."""
    d.setdefault("preferenza_vs_linkedin", None)
    d.setdefault("motivazione_preferenza", "")
    d.setdefault("priorita", "media")
    d.setdefault("confidenza_dati", None)
    d.setdefault("coerenza_fonti", "")
    d.setdefault("riconoscimenti_rilevati", [])
    d.setdefault("sintesi_triangolazione", "")
    d.setdefault("bandierine", [])
    cc = d.get("come_contattarlo") or {}
    if not isinstance(cc, dict):
        cc = {}
    cc.setdefault("canale_consigliato", "LinkedIn")
    cc.setdefault("approccio", "")
    cc.setdefault("bozza_messaggio", "")
    d["come_contattarlo"] = cc
    return d


def triangola(candidato: dict, usa_esterni: bool = False) -> dict:
    """
    Esegue la triangolazione completa di un candidato.

    `usa_esterni=True` forza i lookup OCF/riconoscimenti anche senza le env var
    (usato dalla ricerca smart, dove l'utente ha scelto di attivarli).

    Ritorna:
      { "ok": True,  "dossier": {...}, "fonti": {"ocf": {...}, "riconoscimenti": {...}} }
      { "ok": False, "errore": "messaggio in italiano" }
    """
    nome = (candidato.get("nome") or "").strip()
    cognome = (candidato.get("cognome") or "").strip()

    # Guardrail d'ingresso
    if not cognome:
        return {"ok": False, "errore": "Cognome mancante: impossibile triangolare il contatto."}

    # 1) Verifiche esterne (degradano con grazia: non sollevano mai)
    ris_ocf = ocf_lookup(nome, cognome, contesto=candidato, forza=usa_esterni)
    ris_ric = ric_lookup(nome, cognome, contesto=candidato, forza=usa_esterni)
    ocf_d = ris_ocf.to_dict()
    ric_d = ris_ric.to_dict()
    logger.info("[Triangolazione] %s %s → OCF ok=%s trovato=%s | Ric ok=%s trovato=%s",
                nome, cognome, ris_ocf.ok, ris_ocf.trovato, ris_ric.ok, ris_ric.trovato)

    # 2) Sintesi AI (unica parte che può sollevare → tradotta per l'utente)
    try:
        dossier = triangola_profilo(candidato, ocf_d, ric_d)
        dossier = _valida_dossier(dossier)
    except Exception as e:
        logger.error("[Triangolazione] AI fallita: %s", e, exc_info=True)
        return {"ok": False, "errore": messaggio_errore_ai(e)}

    return {
        "ok": True,
        "dossier": dossier,
        "fonti": {"ocf": ocf_d, "riconoscimenti": ric_d},
    }


def triangola_da_profilo(profilo: dict, usa_esterni: bool = True) -> dict:
    """
    Triangola un profilo appena trovato in ricerca (non ancora in pipeline).
    Adatta il profilo normalizzato (nome/cognome/ruolo/azienda/testo) alla forma
    candidato attesa da triangola(), usando il testo del profilo come base LinkedIn.
    """
    candidato = {
        "nome": profilo.get("nome", ""),
        "cognome": profilo.get("cognome", ""),
        "ruolo_attuale": profilo.get("ruolo_attuale") or profilo.get("ruolo", ""),
        "azienda": profilo.get("azienda", ""),
        "punteggio": profilo.get("punteggio"),
        "analisi": profilo.get("analisi") or profilo.get("testo_profilo") or profilo.get("sommario", ""),
        "profilo_linkedin": profilo.get("url_fonte") or profilo.get("linkedin", ""),
    }
    return triangola(candidato, usa_esterni=usa_esterni)
