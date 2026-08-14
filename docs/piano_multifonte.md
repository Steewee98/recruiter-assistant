# Piano tecnico — Da portale LinkedIn a reclutamento multi-fonte

**Progetto:** SABIA Recruiting Tool (Banca Fideuram)
**Data:** 12/08/2026
**Fonti target scelte:** Albo OCF · Job board (Indeed/InfoJobs) · Fonti di riconoscimento (classifiche/premi/testate)
**Escluse per ora:** AlmaLaurea — i neolaureati senza iscrizione OCF non sono immediatamente utili per il target private banker/consulente abilitato
**Stato:** proposta tecnica — da approvare prima dell'implementazione

---

## 0. Stato implementazione (aggiornato 12/08/2026)

**Già costruito in questa sessione** (fondamenta + triangolazione, tutto additivo e testato):

- `connettori/` — package con il contratto comune (`base.py`), il dispatcher (`__init__.py`)
  e i moduli `linkedin.py` (adapter non invasivo sul codice esistente), `ocf.py`, `riconoscimenti.py`.
- `services/triangolazione.py` — orchestrazione: dato un candidato, incrocia LinkedIn + OCF +
  riconoscimenti e produce un **dossier** ("ci piace più di LinkedIn?" + "come contattarlo?").
- `ai_helpers.triangola_profilo()` — la sintesi AI del dossier (schema JSON validato).
- **Ricerca smart (search-first)** — la pagina `/ricerca` ha ora una **barra stile Google**:
  scrivi in linguaggio naturale (o lasci vuoto → usa il profilo A/B) e ricevi **~20 dossier
  triangolati** che si popolano in progressivo. Endpoint `POST /ricerca/smart/cerca` (fase 1:
  interpreta + trova i profili) e `POST /ricerca/smart/dossier` (fase 2: triangola un profilo).
  Ogni card ha "Aggiungi alla pipeline". Lookup OCF/riconoscimenti **attivi** (best-effort).
- Endpoint `POST /pipeline/triangola/<id>` + pulsante **▲ Triangola** nella pipeline + modal
  (triangolazione anche del singolo candidato già in pipeline).
- DB: colonne additive `url_fonte`, `source` (default linkedin), `fonti_attive`, `triangolazione`,
  `data_triangolazione` (applicate).
- `tests/test_triangolazione.py` — 5 guardrail verdi, **senza chiamare l'API** (nessun costo).

**Guardrail attivi:** i lookup esterni OCF/riconoscimenti sono **disattivati di default**
(`OCF_LOOKUP_ATTIVO=1` / `RICONOSCIMENTI_LOOKUP_ATTIVO=1` per abilitarli). La triangolazione
funziona già oggi producendo il dossier dai dati LinkedIn + AI; quando gli endpoint esterni
saranno confermati dallo spike, si arricchisce da sola.

**Prossimi step:** spike OCF (confermare endpoint/parsing del portale) → estrazione AI dei
riconoscimenti dagli articoli → Fase 2 (Indeed) per il volume.

---

## 1. Obiettivo

Trasformare l'app da strumento mono-fonte (solo LinkedIn via Apify) a **piattaforma
di reclutamento multi-canale**, dove ogni nuova fonte alimenta la stessa pipeline
di valutazione AI, gestione stati e recap già esistente — senza duplicare logica.

Vincolo di design: **non riscrivere la pipeline**. Va estesa l'ingestione, non il resto.

---

## 2. Architettura target — il pattern "Connettore Fonte"

### 2.1 Com'è oggi (mono-fonte, accoppiato)

Il flusso LinkedIn in `routes/ricerca.py` è:

```
cerca_apify()            → chiama l'actor Apify, fa polling, ritorna items grezzi
normalizza_profilo(p)    → estrae nome/ruolo/azienda/testo nel dict candidato
_filtro_qualita / _filtro_locale / _matches_citta  → scarta rumore e fuori-target
[valutazione AI]         → punteggio + analisi + spunti
INSERT INTO candidati    → source = 'linkedin'
```

Il problema: `cerca_apify` e `normalizza_profilo` sono **specifici LinkedIn** ma
chiamati direttamente dagli endpoint. Aggiungere una fonte oggi = copiare tutto.

### 2.2 Come diventa (multi-fonte, disaccoppiato)

Si introduce un'interfaccia comune. Ogni fonte implementa **solo due funzioni**;
tutto il resto (filtri, AI, dedup, insert, pipeline, recap) è condiviso e già scritto.

```
┌─────────────────────────────────────────────────────────────┐
│  CONNETTORI (uno per fonte — l'unica parte nuova)            │
│                                                             │
│  connettori/linkedin.py   cerca() → [raw]   normalizza()    │  (rifattorizzato da ricerca.py)
│  connettori/ocf.py        cerca() → [raw]   normalizza()    │  ← nuovo
│  connettori/jobboard.py   cerca() → [raw]   normalizza()    │  ← nuovo
│  connettori/riconoscimenti.py cerca() → [raw] normalizza()  │  ← nuovo (classifiche/premi)
└───────────────────────────────┬─────────────────────────────┘
                                │  restituiscono TUTTI lo stesso dict candidato
                                ▼
┌─────────────────────────────────────────────────────────────┐
│  CORE CONDIVISO (già esistente, invariato)                  │
│  filtri qualità → dedup cross-fonte → valutazione AI →      │
│  INSERT INTO candidati (source=<fonte>) → pipeline → recap  │
└─────────────────────────────────────────────────────────────┘
```

### 2.3 Il contratto del connettore (interfaccia unica)

Ogni file `connettori/<fonte>.py` espone:

```python
FONTE = "ocf"   # valore che finisce nel campo candidati.source

def cerca(parametri: dict, progress_cb=None) -> tuple[list[dict], str | None]:
    """Ritorna (profili_grezzi, errore). Firma identica a cerca_apify()."""

def normalizza(profilo_grezzo: dict) -> dict:
    """Mappa il grezzo nel dict candidato standard:
       {nome, cognome, ruolo_attuale, azienda, anni_esperienza,
        location, url_fonte, testo_profilo, source}"""
```

Un **dispatcher** unico (`connettori/__init__.py`) instrada per nome fonte:

```python
CONNETTORI = {"linkedin": linkedin, "ocf": ocf, "jobboard": jobboard, "riconoscimenti": riconoscimenti}
def get_connettore(fonte): return CONNETTORI[fonte]
```

Così un solo endpoint di ricerca serve tutte le fonti: cambia solo il parametro `fonte`.

---

## 3. Modifiche al database

Lo schema è **già quasi pronto** (`candidati.source`, `ricerche_automatiche.fonte`,
`valutazioni.fonte`). Servono solo aggiunte non distruttive (pattern `ADD COLUMN IF NOT EXISTS`
già usato in `init_db()`):

| Tabella | Colonna | Motivo |
|---|---|---|
| `candidati` | `url_fonte TEXT` | link generico all'origine (LinkedIn URL diventa un caso di questo) |
| `candidati` | `source` (già esiste) | valorizzare `ocf` / `jobboard` / `almalaurea` |
| `impostazioni_profilo` | `fonti_attive TEXT DEFAULT 'linkedin'` | quali fonti usare per profilo A/B |
| `profili_scartati` | `source TEXT` | blacklist per-fonte |
| `ricerche_automatiche` | `fonte` (già esiste) | tracciare da che canale è partita la ricerca |

**Deduplica cross-fonte** (critica col multi-canale): la stessa persona può arrivare da
LinkedIn *e* OCF. Chiave di dedup proposta:
- match forte su `url_fonte` (già unico per LinkedIn), altrimenti
- match su `lower(nome+cognome) + lower(azienda)` con alert soft al recruiter.

---

## 4. Modifiche UI (minime)

1. **Selettore fonte** nella pagina Ricerca — dropdown/tab: LinkedIn · OCF · Job board · AlmaLaurea.
   I campi del form cambiano per fonte (es. OCF: sezione albo/regione; Job board: keyword+località).
2. **Badge fonte** nella pipeline e nel recap — capire a colpo d'occhio da dove viene ogni candidato.
3. **Filtro per fonte** in pipeline + una riga "per fonte" nel recap (riuso di `genera_recap.py`,
   il campo `source` è già lì).

---

## 5. Fonti — fattibilità tecnica dettagliata

### 5.1 🔵 Albo OCF — Organismo Consulenti Finanziari

**Perché prima:** registro pubblico e ufficiale di tutti i consulenti finanziari abilitati in
Italia. Per reclutare private banker Fideuram è la fonte più mirata: profili **già qualificati**.

| Aspetto | Dettaglio |
|---|---|
| Accesso | ✅ **Confermato pubblico** — portale OCF `organismocf.it` → "Consulta Albo" → "Ricerca nelle sezioni dell'albo". Espone i dati ex art. 146 Reg. Intermediari Consob 20307/18 |
| Metodo tecnico | Connettore HTTP dedicato. Da verificare se esiste endpoint/export o serve un actor Apify custom sul portale di consultazione |
| Campi ottenibili | Nome, cognome, sezione albo, data iscrizione, ente/mandante attuale, regione |
| Valore per SABIA | Altissimo — target esatto, dato verificato |
| Sfide | Il portale è pensato per consultazione singola, non bulk → serve ricerca strutturata per criteri (regione + sezione). Nessun contatto diretto (no email/telefono): l'albo dà l'anagrafica, il contatto va poi cercato |
| Compliance | Dato pubblico ma finalità recruiting va coperta da base giuridica GDPR e informativa. **Da validare con compliance Fideuram** |
| Stima | 3–5 giorni (connettore + mapping campi + verifica accesso) |

> ⚠️ **Verifica bloccante da fare per prima:** capire *come* il portale OCF espone i dati
> (form di ricerca, eventuale API, limiti anti-scraping). Determina se il connettore è un
> semplice client HTTP o un actor Apify. → **1 giorno di spike tecnico prima di stimare in modo definitivo.**

### 5.2 🟡 Job board — Indeed / InfoJobs

**Perché:** stesso identico pattern di LinkedIn (già padroneggiato con Apify), scala il volume.

| Aspetto | Dettaglio |
|---|---|
| Accesso | Actor Apify esistenti per Indeed/InfoJobs (marketplace Apify) |
| Metodo tecnico | Riuso 1:1 dello scheletro `cerca_apify` → cambia solo `APIFY_ACTOR` e la normalizzazione |
| Campi ottenibili | Titolo/ruolo, azienda, località, snippet CV/annuncio, a volte anni esperienza |
| Valore per SABIA | Medio — buono per volume e profili attivi in cerca, meno mirato di OCF |
| Sfide | I job board mescolano *annunci* e *profili*: serve filtrare per candidati/CV, non offerte. Qualità dato variabile |
| Compliance | ToS del portale + GDPR. Preferire actor conformi; evitare scraping aggressivo |
| Stima | 2–4 giorni per il primo (Indeed), ~1 giorno per il secondo (InfoJobs) grazie al pattern condiviso |

### 5.3 🔵 Fonti di riconoscimento — classifiche, premi e testate di settore

**Perché:** è la richiesta esplicita — trovare i **consulenti bravi che si sono già fatti
riconoscere**. Queste fonti non danno volume ma **qualità pre-filtrata**: sono liste di top
performer già selezionati da giurie ed editori di settore. Idealmente si combinano con l'OCF
(per confermare iscrizione + mandante attuale) e LinkedIn (per il contatto).

**Le fonti individuate (ricerca 08/2026):**

| Fonte | Cosa pubblica | Nomi visibili | Valore | Note |
|---|---|---|---|---|
| **Bluerating** (`bluerating.com`) | Bluerating Awards, Private Banking Awards, "pagelle delle reti". Testata dedicata ai consulenti/reti | ✅ Sì, vincitori per rete | **Alto** | La fonte più ricca e verticale in Italia. 33 premi 2025 |
| **Citywire Italia** (`citywire.com/it`) | Wealth Awards + "Top 50 Citywire Italia" consulenti/PB | ✅ Sì | **Alto** | Giuria tecnica indipendente; profili con esperienza dettagliata |
| **Forbes Italia** (`forbes.it`) | Private Banking Awards — vincitori | ✅ Sì (top-tier) | Medio | Pochi nomi ma di altissimo profilo |
| **Milano Finanza / MF** | Classifiche reti + "consulenti più ricchi" | Parziale | Medio | Alcuni contenuti a pagamento |
| **sfadvisor.it** | "Migliori consulenti finanziari" | ✅ Sì (solo ~5) | Basso | Lista curata ma minuscola, non una banca dati |

| Aspetto | Dettaglio |
|---|---|
| Metodo tecnico | Connettore che **estrae i nomi dagli articoli/classifiche** (fetch pagina → AI che struttura nome/premio/rete/anno). Poi arricchimento via OCF + LinkedIn |
| Campi ottenibili | Nome, cognome, premio/classifica, rete/mandante, anno, categoria |
| Valore per SABIA | Alto in **precisione**: candidati già validati da terzi. Basso in **volume** |
| Sfide | Dati dentro articoli non strutturati → serve l'AI per estrarli; alcune testate hanno contenuti a pagamento; aggiornamento periodico (i premi sono annuali) |
| Compliance | Dati pubblicati editorialmente; finalità recruiting comunque coperta da GDPR come le altre fonti |
| Stima | 3–4 giorni (Bluerating + Citywire come prime due; le altre a incremento) |

> **Strategia d'uso consigliata:** trattare queste fonti come un **"segnale di qualità"** che
> alimenta una lista ristretta di eccellenze, non come sorgente di volume. Il flusso ideale:
> classifica/premio → nome → verifica su OCF (iscritto? quale rete?) → LinkedIn (contatto) →
> pipeline. Così unisci *breadth* (OCF/job board) e *depth* (riconoscimenti).

---

## 6. Roadmap consigliata

| Fase | Contenuto | Output | Stima |
|---|---|---|---|
| **0 — Rifattorizzazione** | Estrarre `connettori/linkedin.py` dall'attuale `ricerca.py` dietro il contratto §2.3, senza cambiare comportamento | Base pronta per il multi-fonte, zero regressioni | 2–3 gg |
| **1 — DB + UI multi-fonte** | Colonne §3, selettore fonte, badge, dedup cross-fonte, recap per fonte | App "consapevole delle fonti" | 2–3 gg |
| **2 — Job board (Indeed)** | Primo connettore nuovo, il più semplice (pattern Apify noto) → **valida l'architettura end-to-end** | Seconda fonte attiva | 2–4 gg |
| **3 — Albo OCF** | Spike accesso (1 gg, ✅ portale pubblico confermato) + connettore. Massimo valore per il business | Terza fonte, la più strategica | 4–6 gg |
| **4 — Fonti di riconoscimento** | Connettore Bluerating + Citywire (fetch + estrazione AI), arricchimento via OCF/LinkedIn | Quarta fonte (top performer già validati) | 3–4 gg |

**Perché quest'ordine:** Fase 2 (Indeed) è messa prima di OCF anche se vale meno, perché è
la più semplice e serve a **collaudare tutta la catena multi-fonte** su terreno noto. Una volta
che il pattern regge con Indeed, OCF e le fonti di riconoscimento sono "solo un altro connettore".

---

## 7. Rischi e compliance (spirito critico)

- **GDPR / compliance bancaria** — è il rischio n.1. Si sta costruendo un database di dati
  personali per una banca. Ogni fonte richiede base giuridica e informativa. Coinvolgere il
  compliance Fideuram **prima** della messa in produzione, non dopo.
- **ToS delle fonti** — moltiplicare le fonti moltiplica il rischio legale già presente col
  LinkedIn scraping. Preferire sempre accordo/API a scraping grezzo; per le testate rispettare
  i ToS editoriali (uso dei nomi a fini di contatto professionale, non ripubblicazione).
- **Qualità del dato eterogenea** — OCF dà anagrafica verificata ma niente contatti; i job board
  danno contatti ma dato sporco. La valutazione AI va tarata per non penalizzare fonti con meno campi.
- **Dedup** — senza dedup cross-fonte robusta, lo stesso candidato appare 3 volte e falsa il recap.
- **Dipendenze esterne** — AlmaLaurea e (forse) OCF hanno tempi non controllabili da noi (accordi,
  accessi). Non metterli sul critical path della prima release.

---

## 8. Decisione richiesta prima di partire

1. **Ordine confermato?** (proposto: rifattorizzazione → Indeed → OCF → Fonti di riconoscimento)
2. **Spike OCF**: autorizzi 1 giorno di verifica tecnica accesso portale prima di stimare in modo definitivo?
3. **Compliance Fideuram**: chi è il referente da coinvolgere sulla base giuridica GDPR?
4. **Fonti di riconoscimento**: partiamo da Bluerating + Citywire come prime due testate?
