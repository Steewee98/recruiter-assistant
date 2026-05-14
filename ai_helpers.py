"""
Helper per le chiamate all'API Anthropic Claude.
Contiene tutte le funzioni AI usate dai vari moduli dell'app.
"""

import anthropic
import os
import json
import re
import logging

# Model ID Claude Opus 4.7
CLAUDE_MODEL = "claude-sonnet-4-5"

logger = logging.getLogger(__name__)


def get_client():
    """Crea e restituisce il client Anthropic."""
    return anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))


def clean_text(testo: str) -> str:
    """
    Sanitizza il testo prima di mandarlo all'API:
    - Forza encoding UTF-8 rimuovendo caratteri non validi
    - Normalizza spazi e a capo
    - Rimuove caratteri di controllo (eccetto \\n e \\t)
    """
    if not testo:
        return ""
    # Forza stringa Python, rimuovi caratteri non-UTF8
    if isinstance(testo, bytes):
        testo = testo.decode("utf-8", errors="ignore")
    # Rimuovi caratteri di controllo non stampabili (mantieni \n \t \r)
    testo = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", testo)
    # Normalizza spazi multipli e righe vuote eccessive
    testo = re.sub(r"\n{3,}", "\n\n", testo)
    testo = testo.strip()
    return testo


def _log_payload(funzione: str, payload: dict):
    """Logga il payload completo prima di ogni chiamata API."""
    logger.info(
        "[AI] %s → model=%s max_tokens=%s messages_count=%d first_content_preview=%s",
        funzione,
        payload.get("model"),
        payload.get("max_tokens"),
        len(payload.get("messages", [])),
        str(payload.get("messages", [{}])[0].get("content", ""))[:120],
    )


def _chiama_api(funzione: str, payload: dict):
    """
    Esegue la chiamata all'API Anthropic con logging completo.
    Lancia l'eccezione originale in caso di errore, dopo aver loggato il response body.
    """
    # Validazioni difensive
    assert isinstance(payload["model"], str), "model deve essere una stringa"
    assert isinstance(payload["max_tokens"], int), "max_tokens deve essere un intero"
    assert isinstance(payload["messages"], list) and len(payload["messages"]) > 0, \
        "messages deve essere una lista non vuota"
    for m in payload["messages"]:
        assert isinstance(m.get("role"), str), "ogni messaggio deve avere role stringa"
        assert isinstance(m.get("content"), str), "ogni messaggio deve avere content stringa"

    _log_payload(funzione, payload)

    client = get_client()
    try:
        risposta = client.messages.create(**payload)
        return risposta
    except anthropic.APIStatusError as e:
        logger.error(
            "[AI] %s → APIStatusError status=%s body=%s",
            funzione, e.status_code, e.response.text if hasattr(e, "response") else str(e),
            exc_info=True,
        )
        raise
    except anthropic.APIError as e:
        logger.error("[AI] %s → APIError: %s", funzione, str(e), exc_info=True)
        raise
    except Exception as e:
        logger.error("[AI] %s → %s: %s", funzione, type(e).__name__, str(e), exc_info=True)
        raise


def test_connessione_api() -> dict:
    """
    Chiama l'API con un payload minimale per verificare la connettività.
    Restituisce {"ok": True} oppure {"ok": False, "errore": "..."}.
    """
    payload = {
        "model": CLAUDE_MODEL,
        "max_tokens": 10,
        "messages": [{"role": "user", "content": "test"}],
    }
    try:
        _chiama_api("test_connessione_api", payload)
        logger.info("[AI] test_connessione_api → OK")
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "errore": str(e)}


def analizza_profilo_linkedin(testo_profilo: str, tipo_profilo: str, impostazioni: dict = None) -> dict:
    """
    Analizza un profilo LinkedIn e restituisce valutazione strutturata.
    tipo_profilo: 'A' = Senior, 'B' = Under 35
    """
    testo_profilo = clean_text(testo_profilo)

    if impostazioni:
        if tipo_profilo == "A":
            descrizione_profilo = (
                f"Profilo A (Senior): età tra {impostazioni.get('eta_min', 40)} e "
                f"{impostazioni.get('eta_max', 65)} anni, almeno "
                f"{impostazioni.get('anni_esperienza_min', 5)} anni di esperienza nel settore. "
                f"Settori di provenienza accettati: {impostazioni.get('settori', 'consulenza, bancario, finanziario')}. "
                f"Ruoli target: {impostazioni.get('ruoli_target', '')}. "
                f"Segnali positivi (favoriscono la valutazione): {impostazioni.get('keyword_positive', '')}. "
                f"Segnali negativi (penalizzano la valutazione): {impostazioni.get('keyword_negative', '')}."
            )
        else:
            descrizione_profilo = (
                f"Profilo B (Under 35): età tra {impostazioni.get('eta_min', 22)} e "
                f"{impostazioni.get('eta_max', 35)} anni, almeno "
                f"{impostazioni.get('anni_esperienza_min', 2)} anni di esperienza bancaria. "
                f"Istituti di provenienza preferiti: {impostazioni.get('istituti', 'banche, assicurazioni, SIM')}. "
                f"Ruoli target: {impostazioni.get('ruoli_target', '')}. "
                f"Segnali positivi (favoriscono la valutazione): {impostazioni.get('keyword_positive', '')}. "
                f"Segnali negativi (penalizzano la valutazione): {impostazioni.get('keyword_negative', '')}."
            )
        p_eta = impostazioni.get('peso_eta', 5)
        p_esp = impostazioni.get('peso_esperienza', 5)
        p_set = impostazioni.get('peso_settore', 5)
        p_ruo = impostazioni.get('peso_ruolo', 5)
        p_kw  = impostazioni.get('peso_keyword', 5)
        istr_punteggio = (
            f"\nPer il punteggio finale (1-10) usa questi pesi (scala 0-10, 0=ignora, 10=determinante):\n"
            f"- Eta nel range target: {p_eta}/10\n"
            f"- Anni di esperienza: {p_esp}/10\n"
            f"- Settore/istituto di provenienza: {p_set}/10\n"
            f"- Corrispondenza ruolo target: {p_ruo}/10\n"
            f"- Parole chiave positive/negative: {p_kw}/10"
        )
    else:
        if tipo_profilo == "A":
            descrizione_profilo = (
                "Profilo A: professionista senior tra 40 e 65 anni, "
                "con esperienza in consulenza, libero professionista o con P.IVA, "
                "orientato all'autonomia e alla gestione clienti."
            )
        else:
            descrizione_profilo = (
                "Profilo B: professionista under 35 con esperienza bancaria, "
                "orientato alla crescita professionale nel settore finanziario."
            )
        istr_punteggio = ""

    prompt = (
        "Sei un esperto recruiter nel settore della consulenza finanziaria e bancaria.\n"
        "Analizza il seguente profilo LinkedIn per valutarne la compatibilita con questo target:\n\n"
        f"{descrizione_profilo}{istr_punteggio}\n\n"
        "PROFILO LINKEDIN:\n"
        f"{testo_profilo}\n\n"
        "Fornisci la tua analisi ESCLUSIVAMENTE nel seguente formato JSON valido, senza testo aggiuntivo:\n"
        "{\n"
        '  "nome_contatto": "<nome e cognome della persona estratto dal testo, o null se non identificabile>",\n'
        '  "ruolo_attuale": "<ruolo/titolo professionale attuale estratto dal testo, o null>",\n'
        '  "azienda": "<nome dell\'azienda attuale estratto dal testo, o null>",\n'
        '  "anni_esperienza": <numero intero degli anni totali di esperienza professionale stimati dal testo, o null>,\n'
        '  "punteggio": <numero da 1 a 10>,\n'
        '  "analisi_percorso": "<analisi dettagliata del percorso professionale in 3-4 frasi>",\n'
        '  "spunti_contatto": [\n'
        '    "<spunto personalizzato 1 per il primo contatto>",\n'
        '    "<spunto personalizzato 2 per il primo contatto>",\n'
        '    "<spunto personalizzato 3 per il primo contatto>"\n'
        '  ],\n'
        '  "messaggio_outreach": "<bozza completa del messaggio di outreach personalizzato su LinkedIn, tono professionale ma umano, max 300 caratteri>"\n'
        "}"
    )

    payload = {
        "model":      CLAUDE_MODEL,
        "max_tokens": 1500,
        "messages":   [{"role": "user", "content": clean_text(prompt)}],
    }
    try:
        risposta = _chiama_api("analizza_profilo_linkedin", payload)
        testo = risposta.content[0].text.strip()
        if testo.startswith("```"):
            testo = testo.split("```")[1]
            if testo.startswith("json"):
                testo = testo[4:]
        return json.loads(testo)
    except Exception as e:
        logger.error("[AI] analizza_profilo_linkedin fallita: %s", e)
        raise


def analizza_profilo_arricchito(
    testo_profilo: str,
    tipo_profilo: str,
    dati_prx: dict,
    dati_base: dict,
) -> dict:
    """
    Seconda analisi Claude con dati Proxycurl arricchiti.
    Restituisce i campi predittivi aggiuntivi (punteggio_compatibilita,
    indice_mobilita, pattern_carriera, ecc.).
    In caso di errore restituisce un dict vuoto (il caller usa i dati base).
    """
    from proxycurl_helpers import estrai_testo_proxycurl

    testo_prx = estrai_testo_proxycurl(dati_prx)
    base_str = json.dumps(
        {k: dati_base.get(k) for k in ["punteggio", "analisi_percorso", "ruolo_attuale", "azienda", "anni_esperienza"]},
        ensure_ascii=False,
    )

    prompt = (
        "Sei un esperto recruiter nel settore della consulenza finanziaria e bancaria.\n"
        "Disponi di un'analisi base già effettuata e di dati arricchiti da Proxycurl.\n\n"
        f"ANALISI BASE:\n{base_str}\n\n"
        f"TESTO PROFILO:\n{clean_text(testo_profilo)[:1500]}\n\n"
        f"DATI ARRICCHITI PROXYCURL:\n{testo_prx}\n\n"
        "Fornisci una valutazione predittiva completa ESCLUSIVAMENTE in questo formato JSON valido:\n"
        "{\n"
        '  "punteggio_finale": <1-10, aggiornato con tutti i dati>,\n'
        '  "punteggio_compatibilita": <1-10>,\n'
        '  "indice_mobilita": <1-10, probabilità di essere aperto a nuove opportunità>,\n'
        '  "punteggio_qualita_profilo": <1-10, completezza e cura del profilo LinkedIn>,\n'
        '  "pattern_carriera": "<stabile|dinamico|in_stallo|in_crescita|instabile>",\n'
        '  "momento_contatto": "<ora|6_mesi|1_anno|non_adatto>",\n'
        '  "motivazione_probabile": "<1-2 frasi su perché potrebbe essere interessato>",\n'
        '  "segnali_positivi": ["<segnale 1>", "<segnale 2>", "<segnale 3>"],\n'
        '  "segnali_negativi": ["<segnale 1>", "<segnale 2>"],\n'
        '  "rischi": ["<rischio 1>", "<rischio 2>"],\n'
        '  "analisi_attivita": "<analisi breve dell\'attività LinkedIn recente>",\n'
        '  "messaggio_personalizzato": "<bozza messaggio LinkedIn personalizzato, max 300 caratteri>",\n'
        '  "sintesi": "<sintesi del profilo e opportunità in 2-3 righe>"\n'
        "}"
    )

    payload = {
        "model": CLAUDE_MODEL,
        "max_tokens": 2000,
        "messages": [{"role": "user", "content": clean_text(prompt)}],
    }
    try:
        risposta = _chiama_api("analizza_profilo_arricchito", payload)
        testo = risposta.content[0].text.strip()
        if testo.startswith("```"):
            testo = testo.split("```")[1]
            if testo.startswith("json"):
                testo = testo[4:]
        result = json.loads(testo)
        # Usa punteggio_finale per sovrascrivere il punteggio base
        if "punteggio_finale" in result:
            result["punteggio"] = result["punteggio_finale"]
        return result
    except Exception as e:
        logger.error("[AI] analizza_profilo_arricchito fallita: %s", e)
        return {}


def analizza_profilo_linkedin_stream(
    testo_profilo: str,
    tipo_profilo: str,
    impostazioni: dict = None,
    linkedin_url: str = None,
    dati_proxycurl_cached: dict = None,
    punteggio_forzato: int = None,
):
    """
    Versione streaming di analizza_profilo_linkedin().
    Genera eventi SSE:
      - {"type":"chunk","text":"..."}        — testo grezzo in arrivo da Claude
      - {"type":"arricchimento_start"}       — Proxycurl in corso (solo se applicabile)
      - {"type":"done","risultato":{}}        — analisi completa, JSON parsato
      - {"type":"errore","messaggio":"..."}  — in caso di errore

    Se punteggio base >= 6 e linkedin_url disponibile:
      - Chiama Proxycurl (o usa cache se < 30 giorni)
      - Esegue seconda analisi Claude con dati arricchiti
    """
    print(f"=== ANALISI START: linkedin_url={linkedin_url} punteggio_forzato={punteggio_forzato} ===", flush=True)
    testo_profilo = clean_text(testo_profilo)

    # ── Punteggio forzato: salta analisi base e va direttamente all'arricchimento ──
    if punteggio_forzato is not None and punteggio_forzato >= 5:
        print(f"=== PUNTEGGIO FORZATO: {punteggio_forzato} — skip analisi base ===", flush=True)
        risultato = {
            "punteggio": punteggio_forzato,
            "analisi_percorso": "",
            "spunti_contatto": [],
            "messaggio_outreach": "",
            "arricchito": False,
        }
        if linkedin_url or dati_proxycurl_cached:
            yield f"data: {json.dumps({'type': 'arricchimento_start'}, ensure_ascii=False)}\n\n"
            try:
                from proxycurl_helpers import arricchisci_profilo, is_cache_valida
                dati_prx = None
                if dati_proxycurl_cached and is_cache_valida(dati_proxycurl_cached):
                    dati_prx = dati_proxycurl_cached
                    logger.info("[AI] Proxycurl (forzato): uso cache per %s", linkedin_url)
                elif linkedin_url:
                    print(f"=== ENRICHLAYER START: url={linkedin_url} ===", flush=True)
                    dati_prx = arricchisci_profilo(linkedin_url)
                    print(f"=== ENRICHLAYER DONE: campi={len(dati_prx) if dati_prx else 0} ===", flush=True)
                if dati_prx:
                    enriched = analizza_profilo_arricchito(testo_profilo, tipo_profilo, dati_prx, risultato)
                    if enriched:
                        risultato.update(enriched)
                        risultato["arricchito"] = True
                        yield f"data: {json.dumps({'type': '_proxycurl_data', 'dati': dati_prx}, ensure_ascii=False)}\n\n"
            except Exception as e_prx:
                logger.error("[AI] Errore arricchimento forzato: %s", e_prx)
                print(f"=== ENRICHLAYER ERROR (forzato): {e_prx} ===", flush=True)
        risultato.pop("dati_proxycurl", None)
        print(f"=== SSE DONE (forzato): arricchito={risultato.get('arricchito')} ===", flush=True)
        yield f"data: {json.dumps({'type': 'done', 'risultato': risultato}, ensure_ascii=False)}\n\n"
        return

    # Stessa logica di costruzione prompt di analizza_profilo_linkedin
    if impostazioni:
        if tipo_profilo == "A":
            descrizione_profilo = (
                f"Profilo A (Senior): età tra {impostazioni.get('eta_min', 40)} e "
                f"{impostazioni.get('eta_max', 65)} anni, almeno "
                f"{impostazioni.get('anni_esperienza_min', 5)} anni di esperienza nel settore. "
                f"Settori di provenienza accettati: {impostazioni.get('settori', 'consulenza, bancario, finanziario')}. "
                f"Ruoli target: {impostazioni.get('ruoli_target', '')}. "
                f"Segnali positivi: {impostazioni.get('keyword_positive', '')}. "
                f"Segnali negativi: {impostazioni.get('keyword_negative', '')}."
            )
        else:
            descrizione_profilo = (
                f"Profilo B (Under 35): età tra {impostazioni.get('eta_min', 22)} e "
                f"{impostazioni.get('eta_max', 35)} anni, almeno "
                f"{impostazioni.get('anni_esperienza_min', 2)} anni di esperienza bancaria. "
                f"Istituti di provenienza preferiti: {impostazioni.get('istituti', 'banche, assicurazioni, SIM')}. "
                f"Ruoli target: {impostazioni.get('ruoli_target', '')}. "
                f"Segnali positivi: {impostazioni.get('keyword_positive', '')}. "
                f"Segnali negativi: {impostazioni.get('keyword_negative', '')}."
            )
        p_eta = impostazioni.get('peso_eta', 5)
        p_esp = impostazioni.get('peso_esperienza', 5)
        p_set = impostazioni.get('peso_settore', 5)
        p_ruo = impostazioni.get('peso_ruolo', 5)
        p_kw  = impostazioni.get('peso_keyword', 5)
        istr_punteggio = (
            f"\nPer il punteggio finale (1-10) usa questi pesi (scala 0-10):\n"
            f"- Eta nel range target: {p_eta}/10\n"
            f"- Anni di esperienza: {p_esp}/10\n"
            f"- Settore/istituto di provenienza: {p_set}/10\n"
            f"- Corrispondenza ruolo target: {p_ruo}/10\n"
            f"- Parole chiave positive/negative: {p_kw}/10"
        )
    else:
        descrizione_profilo = (
            "Profilo A: professionista senior tra 40 e 65 anni, con esperienza in consulenza, "
            "libero professionista o con P.IVA, orientato all'autonomia e alla gestione clienti."
            if tipo_profilo == "A" else
            "Profilo B: professionista under 35 con esperienza bancaria, "
            "orientato alla crescita professionale nel settore finanziario."
        )
        istr_punteggio = ""

    prompt = (
        "Sei un esperto recruiter nel settore della consulenza finanziaria e bancaria.\n"
        "Analizza il seguente profilo LinkedIn per valutarne la compatibilita con questo target:\n\n"
        f"{descrizione_profilo}{istr_punteggio}\n\n"
        "PROFILO LINKEDIN:\n"
        f"{testo_profilo}\n\n"
        "Fornisci la tua analisi ESCLUSIVAMENTE nel seguente formato JSON valido, senza testo aggiuntivo:\n"
        "{\n"
        '  "nome_contatto": "<nome e cognome, o null>",\n'
        '  "ruolo_attuale": "<ruolo/titolo professionale attuale, o null>",\n'
        '  "azienda": "<nome azienda attuale, o null>",\n'
        '  "anni_esperienza": <numero intero o null>,\n'
        '  "punteggio": <numero da 1 a 10>,\n'
        '  "analisi_percorso": "<analisi dettagliata in 3-4 frasi>",\n'
        '  "spunti_contatto": ["<spunto 1>","<spunto 2>","<spunto 3>"],\n'
        '  "messaggio_outreach": "<bozza messaggio LinkedIn max 300 caratteri>"\n'
        "}"
    )

    payload = {
        "model":      CLAUDE_MODEL,
        "max_tokens": 1500,
        "messages":   [{"role": "user", "content": clean_text(prompt)}],
    }

    client = get_client()
    testo_accumulato = ""

    try:
        with client.messages.stream(**payload) as stream:
            for chunk in stream.text_stream:
                testo_accumulato += chunk
                yield f"data: {json.dumps({'type': 'chunk', 'text': chunk}, ensure_ascii=False)}\n\n"

        # Stream completato — parse JSON finale
        testo_pulito = testo_accumulato.strip()
        if testo_pulito.startswith("```"):
            testo_pulito = testo_pulito.split("```")[1]
            if testo_pulito.startswith("json"):
                testo_pulito = testo_pulito[4:]
        risultato = json.loads(testo_pulito)

        # ── Arricchimento Proxycurl (solo se punteggio >= 6 e URL disponibile) ──
        risultato["arricchito"] = False
        punteggio_base = risultato.get("punteggio", 0) or 0
        print(f"=== PUNTEGGIO BASE: {punteggio_base} ===", flush=True)
        if punteggio_base >= 5 and (linkedin_url or dati_proxycurl_cached):
            yield f"data: {json.dumps({'type': 'arricchimento_start'}, ensure_ascii=False)}\n\n"
            try:
                from proxycurl_helpers import arricchisci_profilo, is_cache_valida
                dati_prx = None
                # Usa cache se valida
                if dati_proxycurl_cached and is_cache_valida(dati_proxycurl_cached):
                    dati_prx = dati_proxycurl_cached
                    logger.info("[AI] Proxycurl: uso cache per %s", linkedin_url)
                elif linkedin_url:
                    print(f"=== ENRICHLAYER START: url={linkedin_url} ===", flush=True)
                    dati_prx = arricchisci_profilo(linkedin_url)
                    print(f"=== ENRICHLAYER DONE: campi={len(dati_prx) if dati_prx else 0} ===", flush=True)
                else:
                    print(f"=== ENRICHLAYER SKIP: nessun linkedin_url disponibile ===", flush=True)

                if dati_prx:
                    enriched = analizza_profilo_arricchito(testo_profilo, tipo_profilo, dati_prx, risultato)
                    if enriched:
                        risultato.update(enriched)
                        risultato["arricchito"] = True
                        # Tieni dati_proxycurl separato: yield come evento interno
                        # (intercettato dal route, non inoltrato al browser)
                        yield f"data: {json.dumps({'type': '_proxycurl_data', 'dati': dati_prx}, ensure_ascii=False)}\n\n"
            except Exception as e_prx:
                logger.error("[AI] Errore arricchimento Proxycurl: %s", e_prx)
                print(f"=== ENRICHLAYER ERROR: {e_prx} ===", flush=True)
        else:
            print(f"=== ENRICHLAYER SKIP: punteggio={punteggio_base} linkedin_url={linkedin_url} ===", flush=True)

        # done event senza dati_proxycurl (evita payload > 20 KB che rompe JSON.parse nel browser)
        risultato.pop("dati_proxycurl", None)
        arricchito = risultato.get("arricchito", False)
        print(f"=== SSE DONE: arricchito={arricchito} campi={list(risultato.keys())} ===", flush=True)
        yield f"data: {json.dumps({'type': 'done', 'risultato': risultato}, ensure_ascii=False)}\n\n"

    except json.JSONDecodeError as e:
        logger.error("[AI] analizza_profilo_linkedin_stream — JSON parse fallito: %s | testo: %s", e, testo_accumulato[:200])
        yield f"data: {json.dumps({'type': 'errore', 'messaggio': 'Risposta AI non parsabile come JSON.'})}\n\n"
    except Exception as e:
        logger.error("[AI] analizza_profilo_linkedin_stream — errore: %s", e, exc_info=True)
        yield f"data: {json.dumps({'type': 'errore', 'messaggio': str(e)})}\n\n"


def rigenera_messaggio_outreach(testo_profilo: str, messaggio_attuale: str, istruzioni: str = "") -> str:
    """Rigenera il messaggio di outreach (nuova variante o riscritto con istruzioni)."""
    testo_profilo    = clean_text(testo_profilo)
    messaggio_attuale = clean_text(messaggio_attuale)
    istruzioni        = clean_text(istruzioni)

    if istruzioni:
        prompt = (
            "Sei un recruiter esperto. Riscrivi il seguente messaggio di outreach LinkedIn\n"
            f"seguendo queste istruzioni: {istruzioni}\n\n"
            "MESSAGGIO ATTUALE:\n"
            f"{messaggio_attuale}\n\n"
            "PROFILO DEL CANDIDATO (contesto):\n"
            f"{testo_profilo[:500]}\n\n"
            "Riscrivi SOLO il testo del messaggio, senza intestazioni o spiegazioni. Max 300 caratteri."
        )
    else:
        prompt = (
            "Sei un recruiter esperto. Genera una NUOVA variante del messaggio di outreach LinkedIn\n"
            "per questo candidato. Deve essere diversa dalla versione precedente, mantenendo tono professionale e umano.\n\n"
            "PROFILO DEL CANDIDATO:\n"
            f"{testo_profilo[:500]}\n\n"
            "VERSIONE PRECEDENTE (da cui differenziarti):\n"
            f"{messaggio_attuale}\n\n"
            "Scrivi SOLO il testo del nuovo messaggio, senza intestazioni o spiegazioni. Max 300 caratteri."
        )

    payload = {
        "model":      CLAUDE_MODEL,
        "max_tokens": 400,
        "messages":   [{"role": "user", "content": clean_text(prompt)}],
    }
    risposta = _chiama_api("rigenera_messaggio_outreach", payload)
    return risposta.content[0].text.strip()


def rigenera_messaggio_followup(candidato: dict, messaggio_attuale: str, istruzioni: str = "") -> str:
    """Rigenera o riscrive un messaggio di follow-up."""
    messaggio_attuale = clean_text(messaggio_attuale)
    istruzioni        = clean_text(istruzioni)

    contesto = (
        f"Candidato: {candidato.get('nome','')} {candidato.get('cognome','')}\n"
        f"Ruolo: {candidato.get('ruolo_attuale','N/D')}\n"
        f"Azienda: {candidato.get('azienda','N/D')}\n"
        f"Stato nel processo: {candidato.get('stato','N/D')}"
    )

    if istruzioni:
        prompt = (
            "Sei un recruiter esperto. Riscrivi questo messaggio di follow-up LinkedIn\n"
            f"seguendo queste istruzioni: {istruzioni}\n\n"
            "MESSAGGIO ATTUALE:\n"
            f"{messaggio_attuale}\n\n"
            "CONTESTO CANDIDATO:\n"
            f"{contesto}\n\n"
            "Scrivi SOLO il testo del messaggio, senza intestazioni. Max 300 caratteri."
        )
    else:
        prompt = (
            "Sei un recruiter esperto. Genera una NUOVA variante del messaggio di follow-up\n"
            "LinkedIn, diversa dalla precedente, per questo candidato.\n\n"
            "CONTESTO CANDIDATO:\n"
            f"{contesto}\n\n"
            "VERSIONE PRECEDENTE (da cui differenziarti):\n"
            f"{messaggio_attuale}\n\n"
            "Scrivi SOLO il testo del nuovo messaggio, senza intestazioni. Max 300 caratteri."
        )

    payload = {
        "model":      CLAUDE_MODEL,
        "max_tokens": 400,
        "messages":   [{"role": "user", "content": clean_text(prompt)}],
    }
    risposta = _chiama_api("rigenera_messaggio_followup", payload)
    return risposta.content[0].text.strip()


def genera_messaggio_followup(candidato: dict) -> str:
    """Genera un messaggio di follow-up personalizzato per un candidato."""
    prompt = (
        "Sei un recruiter esperto. Scrivi un breve messaggio di follow-up LinkedIn per questo candidato.\n\n"
        f"Candidato: {candidato['nome']} {candidato['cognome']}\n"
        f"Ruolo attuale: {candidato.get('ruolo_attuale', 'N/D')}\n"
        f"Azienda: {candidato.get('azienda', 'N/D')}\n"
        f"Stato nel processo: {candidato.get('stato', 'N/D')}\n"
        f"Note: {candidato.get('note', 'nessuna')}\n\n"
        "Scrivi SOLO il testo del messaggio, senza intestazioni o spiegazioni.\n"
        "Tono professionale e cordiale. Max 300 caratteri."
    )

    payload = {
        "model":      CLAUDE_MODEL,
        "max_tokens": 400,
        "messages":   [{"role": "user", "content": clean_text(prompt)}],
    }
    risposta = _chiama_api("genera_messaggio_followup", payload)
    return risposta.content[0].text.strip()


def genera_prompt_immagine(testo_post: str, tema: str, tono: str, prompt_custom: str = "") -> str:
    """Usa Claude per costruire un prompt ottimizzato per Pollinations/FLUX."""
    testo_post    = clean_text(testo_post)
    prompt_custom = clean_text(prompt_custom)
    istruzioni_custom = f"\nModifica richiesta dall'utente: {prompt_custom}" if prompt_custom else ""

    prompt = (
        "Sei un esperto di prompt engineering per generatori di immagini AI.\n"
        "Crea un prompt in inglese per generare un'immagine professionale da usare come visual su LinkedIn.\n\n"
        f"TEMA DEL POST: {tema}\n"
        f"TONO: {tono}\n"
        "TESTO DEL POST (riferimento):\n"
        f"{testo_post[:400]}{istruzioni_custom}\n\n"
        "Il prompt deve descrivere un'immagine:\n"
        "- Professionale, moderna, adatta a LinkedIn\n"
        "- Stile fotografico o illustrativo high-end\n"
        "- Colori prevalenti: blu scuro e azzurro professionale\n"
        "- NO testo nell'immagine\n"
        "- NO persone riconoscibili\n"
        "- Concettuale, evocativa del tema\n\n"
        "Rispondi SOLO con il prompt in inglese, max 200 caratteri, senza virgolette."
    )

    payload = {
        "model":      CLAUDE_MODEL,
        "max_tokens": 300,
        "messages":   [{"role": "user", "content": clean_text(prompt)}],
    }
    risposta = _chiama_api("genera_prompt_immagine", payload)
    return risposta.content[0].text.strip()


def modifica_variante_linkedin(testo_attuale: str, richiesta: str) -> str:
    """Modifica una variante di post LinkedIn secondo le istruzioni dell'utente."""
    testo_attuale = clean_text(testo_attuale)
    richiesta = clean_text(richiesta)

    prompt = (
        "Sei un esperto di content marketing LinkedIn nel settore del recruiting bancario.\n"
        "Riscrivi il seguente post LinkedIn applicando la modifica richiesta dall'utente.\n"
        "Mantieni lo stesso stile, tono e struttura a meno che la modifica non richieda diversamente.\n\n"
        f"POST ATTUALE:\n{testo_attuale}\n\n"
        f"MODIFICA RICHIESTA: {richiesta}\n\n"
        "Rispondi SOLO con il testo del post riscritto, senza spiegazioni o commenti."
    )

    payload = {
        "model":      CLAUDE_MODEL,
        "max_tokens": 1500,
        "messages":   [{"role": "user", "content": clean_text(prompt)}],
    }
    risposta = _chiama_api("modifica_variante_linkedin", payload)
    return risposta.content[0].text.strip()


def genera_contenuti_linkedin(tema: str, tono: str, profilo: str,
                               obiettivo: str = "", contesto: str = "") -> dict:
    """Genera 3 varianti di post LinkedIn con tono di voce personalizzato per profilo."""
    # Tono di voce distinto per ogni profilo
    profili_voce = {
        "Salvatore Sabia": (
            "Salvatore Sabia, Regional Manager Fideuram. "
            "Tono autorevole e riflessivo, con esperienza decennale nel settore finanziario. "
            "Parla di leadership, crescita professionale e visione strategica. "
            "Scrive come un leader che condivide la propria esperienza per ispirare."
        ),
        "Firdaous Filahi": (
            "Firdaous Filahi, recruiter specializzata nel settore bancario. "
            "Tono autentico e in evoluzione, ex bancaria ora recruiter. "
            "Parla la lingua di chi lavora in banca, con un tono da pari a pari. "
            "Scrive come qualcuno che capisce davvero il percorso dei professionisti bancari."
        ),
    }
    descrizioni_tono = {
        "relazionale": "caldo, empatico, che crea connessione e invita al dialogo",
        "professionale": "autorevole, preciso, basato su dati e risultati concreti",
        "diretto": "diretto e provocatorio, che sfida lo status quo e fa riflettere",
    }
    obiettivi_desc = {
        "attirare_candidati": "Attirare professionisti bancari verso nuove opportunita di carriera in Fideuram",
        "insight": "Condividere un'intuizione o riflessione sul settore bancario/finanziario",
        "caso_successo": "Raccontare un caso di successo concreto (senza nomi reali) nel recruiting bancario",
        "educare": "Educare il pubblico su un tema del settore finanziario o del recruiting",
        "autorevolezza": "Costruire autorevolezza e posizionamento come esperto del settore",
        "racconto": "Condividere un racconto personale o un'esperienza di vita professionale",
    }

    voce = profili_voce.get(profilo, profilo)
    desc_tono = descrizioni_tono.get(tono, tono)
    desc_obiettivo = obiettivi_desc.get(obiettivo, obiettivo) if obiettivo else ""

    prompt = (
        "Sei un esperto di content marketing LinkedIn nel settore del recruiting bancario "
        "e della consulenza finanziaria.\n\n"
        "Scrivi 3 varianti di post LinkedIn con queste caratteristiche:\n"
        f"- Autore (scrivi in prima persona): {voce}\n"
        f"- Obiettivo: {desc_obiettivo}\n"
        f"- Tema: {tema}\n"
        f"- Tono: {desc_tono}\n"
    )
    if contesto:
        prompt += f"- Contesto aggiuntivo: {contesto}\n"
    prompt += (
        "\nContesto di fondo: il progetto e Fideuram / recruiting bancario. "
        "Salvatore Sabia e Regional Manager e cerca di attrarre professionisti dalla banca. "
        "Firdaous Filahi e la recruiter che supporta il processo.\n\n"
        "Ogni post deve avere:\n"
        "1. Un HOOK d'apertura forte (prima riga che cattura l'attenzione)\n"
        "2. Un CORPO con il messaggio principale (usa paragrafi brevi, frasi d'impatto)\n"
        "3. Una CALL TO ACTION finale\n"
        "4. Massimo 3-5 hashtag pertinenti alla fine\n\n"
        "Lunghezza ideale: 800-1200 caratteri per variante.\n"
        "Le 3 varianti devono essere DIVERSE tra loro nello stile e nell'approccio.\n\n"
        "Fornisci il risultato ESCLUSIVAMENTE in questo formato JSON valido:\n"
        "{\n"
        '  "variante_1": "<testo completo del post 1, con a capo come \\n>",\n'
        '  "variante_2": "<testo completo del post 2, con a capo come \\n>",\n'
        '  "variante_3": "<testo completo del post 3, con a capo come \\n>"\n'
        "}"
    )

    payload = {
        "model":      CLAUDE_MODEL,
        "max_tokens": 3000,
        "messages":   [{"role": "user", "content": clean_text(prompt)}],
    }
    try:
        risposta = _chiama_api("genera_contenuti_linkedin", payload)
        testo = risposta.content[0].text.strip()
        if testo.startswith("```"):
            testo = testo.split("```")[1]
            if testo.startswith("json"):
                testo = testo[4:]
        return json.loads(testo)
    except Exception as e:
        logger.error("[AI] genera_contenuti_linkedin fallita: %s", e)
        raise
