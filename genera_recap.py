"""
Genera i deliverable di recap dei contatti SABIA:
  1. export/contatti_export.csv  — tutti i contatti (apribile in Excel/Numbers)
  2. export/recap_contatti.html  — CRUSCOTTO analitico con grafici, stampabile in PDF

Uso:  venv/bin/python genera_recap.py
"""

import os
import re
import csv
import json
import math
import html
from datetime import datetime
from collections import Counter, defaultdict
from dotenv import load_dotenv

load_dotenv()
from database import get_db

# ── Significato degli stati nel funnel di selezione ──────────────────────────
# Da valutare       → profilo importato, ancora da valutare/contattare
# Richiesta Inviata → richiesta di collegamento LinkedIn inviata (1° contatto)
# Messaggio Inviato → collegamento accettato + messaggio diretto inviato (risposta positiva)
# Chiuso            → processo chiuso (assunto, scartato o non proseguito)
ORDINE_STATI = ["Da valutare", "Richiesta Inviata", "Messaggio Inviato", "Chiuso"]
COLORI_STATO = {
    "Da valutare": "#f0b429", "Richiesta Inviata": "#2E7CF6",
    "Messaggio Inviato": "#16a34a", "Chiuso": "#94a3b8",
}
PALETTE = ["#2E7CF6", "#1A2E4A", "#16a34a", "#f0b429", "#dc2626",
           "#8b5cf6", "#0891b2", "#64748b", "#e879f9", "#f97316"]

OUT_DIR = os.path.join(os.path.dirname(__file__), "export")
os.makedirs(OUT_DIR, exist_ok=True)


# ─────────────────────────────────────────────────────────────────────────────
# Dati
# ─────────────────────────────────────────────────────────────────────────────

def carica_dati():
    db = get_db()
    righe = db.execute(
        """SELECT id, nome, cognome, ruolo_attuale, azienda, anni_esperienza,
                  tipo_profilo, stato, punteggio, gestore, profilo_linkedin,
                  data_inserimento, data_aggiornamento, note, dati_arricchiti
           FROM candidati
           ORDER BY data_inserimento ASC"""
    ).fetchall()
    db.close()
    return [dict(r) for r in righe]


def categoria_ruolo(ruolo: str) -> str:
    """Normalizza il ruolo libero in categorie omogenee (case-insensitive)."""
    r = (ruolo or "").lower()
    if not r.strip():
        return "Non indicato"
    if "private banker" in r:
        return "Private Banker"
    if "wealth" in r:
        return "Wealth Manager"
    if "patrimonial" in r:
        return "Consulente Patrimoniale"
    if "consulente finanziar" in r or "financial advisor" in r or "consulenza finanziar" in r:
        return "Consulente Finanziario"
    if "gestore" in r or "asset manag" in r:
        return "Gestore / Asset Manager"
    if "director" in r or "head" in r or "responsabile" in r or "manager" in r:
        return "Manager / Direzione"
    return "Altro"


def estrai_arricchimento(contatti):
    """Estrae le dimensioni AI da dati_arricchiti (dove presente)."""
    mobilita = Counter()          # bassa / media / alta
    pattern = Counter()           # stabile / in stallo / ...
    momento = Counter()           # ora / 6 mesi / 1 anno
    n_arr = 0
    for c in contatti:
        raw = c.get("dati_arricchiti")
        if not raw:
            continue
        try:
            d = json.loads(raw)
        except Exception:
            continue
        if not isinstance(d, dict):
            continue
        n_arr += 1
        # Indice mobilità 1-10 → fasce
        im = d.get("indice_mobilita")
        if isinstance(im, (int, float)):
            if im <= 3:
                mobilita["Bassa (1-3)"] += 1
            elif im <= 6:
                mobilita["Media (4-6)"] += 1
            else:
                mobilita["Alta (7-10)"] += 1
        # Pattern carriera
        pc = str(d.get("pattern_carriera", "")).replace("_", " ").strip().capitalize()
        if pc:
            pattern[pc] += 1
        # Momento contatto
        mc = str(d.get("momento_contatto", "")).replace("_", " ").strip()
        etichette = {"ora": "Subito", "6 mesi": "Entro 6 mesi", "1 anno": "Entro 1 anno"}
        mc = etichette.get(mc, mc.capitalize())
        if mc:
            momento[mc] += 1
    return {"n_arr": n_arr, "mobilita": mobilita, "pattern": pattern, "momento": momento}


def calcola_statistiche(contatti):
    tot = len(contatti)
    per_stato = Counter(c["stato"] for c in contatti)
    per_gestore = Counter(c["gestore"] or "Non assegnato" for c in contatti)
    per_ruolo = Counter(categoria_ruolo(c["ruolo_attuale"]) for c in contatti)

    da_valutare = per_stato.get("Da valutare", 0)
    contattati = tot - da_valutare
    risposte = per_stato.get("Messaggio Inviato", 0)
    chiusi = per_stato.get("Chiuso", 0)
    tasso_risposta = (risposte / contattati * 100) if contattati else 0

    # Punteggio: distribuzione grezza 1-10 + fasce
    punteggi = [c["punteggio"] for c in contatti if c["punteggio"] is not None]
    dist_punteggio = Counter(punteggi)
    fasce_punteggio = Counter()
    for p in punteggi:
        if p <= 4:
            fasce_punteggio["Basso (1-4)"] += 1
        elif p <= 6:
            fasce_punteggio["Medio (5-6)"] += 1
        elif p <= 8:
            fasce_punteggio["Alto (7-8)"] += 1
        else:
            fasce_punteggio["Top (9-10)"] += 1
    media = sum(punteggi) / len(punteggi) if punteggi else 0

    # Top aziende
    aziende = Counter(c["azienda"].strip() for c in contatti
                      if c.get("azienda") and c["azienda"].strip())

    per_mese = Counter(str(c["data_inserimento"])[:7] for c in contatti)

    # Andamento cumulato per mese
    cumulato, run = [], 0
    for m in sorted(per_mese):
        run += per_mese[m]
        cumulato.append((m, run))

    # Punteggio medio per tipologia di figura
    somma_ruolo, cnt_ruolo = defaultdict(int), defaultdict(int)
    for c in contatti:
        if c["punteggio"] is not None:
            cat = categoria_ruolo(c["ruolo_attuale"])
            somma_ruolo[cat] += c["punteggio"]
            cnt_ruolo[cat] += 1
    media_ruolo = {k: somma_ruolo[k] / cnt_ruolo[k] for k in cnt_ruolo if cnt_ruolo[k] >= 3}

    # Tasso di risposta per gestore (contattati → messaggio inviato)
    conv_gestore = []
    for g in {c["gestore"] or "Non assegnato" for c in contatti}:
        cg = [c for c in contatti if (c["gestore"] or "Non assegnato") == g]
        cont = sum(1 for c in cg if c["stato"] != "Da valutare")
        risp = sum(1 for c in cg if c["stato"] == "Messaggio Inviato")
        if cont:
            conv_gestore.append((g, cont, risp, risp / cont * 100))
    conv_gestore.sort(key=lambda x: x[1], reverse=True)

    # Tipo profilo A/B
    per_tipo = Counter(c["tipo_profilo"] or "?" for c in contatti)

    arr = estrai_arricchimento(contatti)

    return {
        "tot": tot, "da_valutare": da_valutare, "contattati": contattati,
        "risposte": risposte, "chiusi": chiusi, "tasso_risposta": tasso_risposta,
        "per_stato": per_stato, "per_gestore": per_gestore, "per_ruolo": per_ruolo,
        "dist_punteggio": dist_punteggio, "fasce_punteggio": fasce_punteggio,
        "media": media, "aziende": aziende, "per_mese": per_mese, "arr": arr,
        "n_con_azienda": sum(aziende.values()), "cumulato": cumulato,
        "media_ruolo": media_ruolo, "conv_gestore": conv_gestore, "per_tipo": per_tipo,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Grafici SVG (nessuna dipendenza — rendono perfetti in PDF)
# ─────────────────────────────────────────────────────────────────────────────

def donut_svg(data, size=190, thickness=32, centro=None):
    """data: lista (label, valore, colore). Ritorna SVG stringa."""
    tot = sum(v for _, v, _ in data) or 1
    r = (size - thickness) / 2
    cx = cy = size / 2
    C = 2 * math.pi * r
    segs, off = [], 0.0
    for _, val, col in data:
        seg = (val / tot) * C
        segs.append(
            f'<circle cx="{cx}" cy="{cy}" r="{r}" fill="none" stroke="{col}" '
            f'stroke-width="{thickness}" stroke-dasharray="{seg:.2f} {C - seg:.2f}" '
            f'stroke-dashoffset="{-off:.2f}" transform="rotate(-90 {cx} {cy})"/>'
        )
        off += seg
    big = centro if centro is not None else str(tot)
    testo = (f'<text x="{cx}" y="{cy - 2}" text-anchor="middle" font-size="30" '
             f'font-weight="700" fill="#1A2E4A">{big}</text>'
             f'<text x="{cx}" y="{cy + 17}" text-anchor="middle" font-size="11" '
             f'fill="#64748b">totale</text>')
    return f'<svg width="{size}" height="{size}" viewBox="0 0 {size} {size}">{"".join(segs)}{testo}</svg>'


def linea_svg(punti, larghezza=640, altezza=180, colore="#2E7CF6"):
    """punti: lista (label, valore). Grafico ad area con linea (cumulato)."""
    if not punti:
        return ""
    pad_l, pad_b, pad_t, pad_r = 40, 26, 16, 16
    w_plot = larghezza - pad_l - pad_r
    h_plot = altezza - pad_b - pad_t
    vmax = max(v for _, v in punti) or 1
    n = len(punti)
    def x(i): return pad_l + (w_plot * (i / (n - 1) if n > 1 else 0.5))
    def y(v): return pad_t + h_plot * (1 - v / vmax)
    pts = [(x(i), y(v)) for i, (_, v) in enumerate(punti)]
    linea = " ".join(f"{px:.1f},{py:.1f}" for px, py in pts)
    area = f"{pad_l},{pad_t + h_plot} " + linea + f" {pad_l + w_plot:.1f},{pad_t + h_plot}"
    # griglia orizzontale + etichette y
    griglia = ""
    for frac in (0, 0.5, 1):
        gy = pad_t + h_plot * frac
        val = int(round(vmax * (1 - frac)))
        griglia += (f'<line x1="{pad_l}" y1="{gy:.1f}" x2="{pad_l + w_plot}" y2="{gy:.1f}" '
                    f'stroke="#eef2f7" stroke-width="1"/>'
                    f'<text x="{pad_l - 8}" y="{gy + 3:.1f}" text-anchor="end" font-size="10" fill="#94a3b8">{val}</text>')
    # etichette x + punti
    label_x, dots = "", ""
    for i, (lbl, v) in enumerate(punti):
        label_x += (f'<text x="{x(i):.1f}" y="{altezza - 6}" text-anchor="middle" '
                    f'font-size="10" fill="#64748b">{html.escape(lbl)}</text>')
        dots += f'<circle cx="{x(i):.1f}" cy="{y(v):.1f}" r="3.5" fill="{colore}"/>'
        dots += (f'<text x="{x(i):.1f}" y="{y(v) - 8:.1f}" text-anchor="middle" '
                 f'font-size="10" font-weight="700" fill="#1A2E4A">{v}</text>')
    return (f'<svg width="100%" viewBox="0 0 {larghezza} {altezza}">{griglia}'
            f'<polygon points="{area}" fill="{colore}" fill-opacity="0.10"/>'
            f'<polyline points="{linea}" fill="none" stroke="{colore}" stroke-width="2.5" '
            f'stroke-linejoin="round" stroke-linecap="round"/>{dots}{label_x}</svg>')


def legenda_html(data):
    righe = ""
    tot = sum(v for _, v, _ in data) or 1
    for label, val, col in data:
        pct = val / tot * 100
        righe += (f'<div class="leg-row"><span class="dot" style="background:{col}"></span>'
                  f'<span class="leg-lbl">{html.escape(label)}</span>'
                  f'<span class="leg-val">{val} · {pct:.0f}%</span></div>')
    return f'<div class="legenda">{righe}</div>'


def barre_html(coppie, colore="#2E7CF6", mostra_pct=True):
    """coppie: lista (label, valore). Barre orizzontali in HTML."""
    massimo = max((v for _, v in coppie), default=1) or 1
    tot = sum(v for _, v in coppie) or 1
    righe = ""
    for label, val in coppie:
        w = val / massimo * 100
        pct = f' · {val / tot * 100:.0f}%' if mostra_pct else ''
        righe += (
            f'<div class="brow"><div class="blbl">{html.escape(str(label))}</div>'
            f'<div class="btrack"><div class="bfill" style="width:{w:.0f}%;background:{colore}"></div></div>'
            f'<div class="bval">{val}{pct}</div></div>'
        )
    return f'<div class="barre">{righe}</div>'


# ─────────────────────────────────────────────────────────────────────────────
# Output
# ─────────────────────────────────────────────────────────────────────────────

def scrivi_csv(contatti):
    path = os.path.join(OUT_DIR, "contatti_export.csv")
    colonne = [
        ("id", "ID"), ("nome", "Nome"), ("cognome", "Cognome"),
        ("ruolo_attuale", "Ruolo attuale"), ("azienda", "Azienda"),
        ("anni_esperienza", "Anni esperienza"), ("tipo_profilo", "Tipo profilo"),
        ("stato", "Stato"), ("punteggio", "Punteggio"), ("gestore", "Gestore"),
        ("profilo_linkedin", "LinkedIn"), ("data_inserimento", "Data inserimento"),
        ("data_aggiornamento", "Ultimo aggiornamento"), ("note", "Note"),
    ]
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow([label for _, label in colonne])
        for c in contatti:
            w.writerow([(c.get(k) if c.get(k) is not None else "") for k, _ in colonne])
    return path


def scrivi_html(contatti, s):
    oggi = datetime.now().strftime("%d/%m/%Y")

    # Donut stato
    dati_stato = [(st, s["per_stato"].get(st, 0), COLORI_STATO[st]) for st in ORDINE_STATI]
    donut_stato = donut_svg(dati_stato)
    legenda_stato = legenda_html(dati_stato)

    # Donut categorie ruolo
    ruoli_ord = s["per_ruolo"].most_common()
    dati_ruolo = [(lbl, val, PALETTE[i % len(PALETTE)]) for i, (lbl, val) in enumerate(ruoli_ord)]
    donut_ruolo = donut_svg(dati_ruolo, centro=str(s["tot"]))
    legenda_ruolo = legenda_html(dati_ruolo)

    # Barre punteggio 1-10
    coppie_pun = [(str(p), s["dist_punteggio"].get(p, 0)) for p in range(1, 11)]
    barre_pun = barre_html(coppie_pun, colore="#2E7CF6", mostra_pct=False)

    # Fasce qualità
    ordine_fasce = ["Basso (1-4)", "Medio (5-6)", "Alto (7-8)", "Top (9-10)"]
    dati_fasce = [(f, s["fasce_punteggio"].get(f, 0),
                   {"Basso (1-4)": "#dc2626", "Medio (5-6)": "#f0b429",
                    "Alto (7-8)": "#2E7CF6", "Top (9-10)": "#16a34a"}[f])
                  for f in ordine_fasce if s["fasce_punteggio"].get(f, 0)]
    donut_qual = donut_svg(dati_fasce, centro=f"{s['media']:.1f}")
    legenda_qual = legenda_html(dati_fasce)

    # Top aziende
    top_az = s["aziende"].most_common(10)
    barre_az = barre_html(top_az, colore="#1A2E4A") if top_az else "<p class='vuoto'>Dato azienda non disponibile per la maggior parte dei profili.</p>"

    # Intelligence AI
    arr = s["arr"]
    ord_mob = ["Bassa (1-3)", "Media (4-6)", "Alta (7-10)"]
    col_mob = {"Bassa (1-3)": "#dc2626", "Media (4-6)": "#f0b429", "Alta (7-10)": "#16a34a"}
    barre_mob = barre_html([(m, arr["mobilita"].get(m, 0)) for m in ord_mob if arr["mobilita"].get(m, 0)], colore="#16a34a")
    barre_pattern = barre_html(arr["pattern"].most_common(), colore="#8b5cf6")
    ord_mom = ["Subito", "Entro 6 mesi", "Entro 1 anno"]
    momenti = [(m, arr["momento"].get(m, 0)) for m in ord_mom if arr["momento"].get(m, 0)]
    momenti += [(m, v) for m, v in arr["momento"].most_common() if m not in ord_mom]
    barre_momento = barre_html(momenti, colore="#0891b2")

    # Gestori
    barre_gestore = barre_html(s["per_gestore"].most_common(), colore="#2E7CF6")

    # Timeline mesi
    mesi_it = {"01": "Gen", "02": "Feb", "03": "Mar", "04": "Apr", "05": "Mag", "06": "Giu",
               "07": "Lug", "08": "Ago", "09": "Set", "10": "Ott", "11": "Nov", "12": "Dic"}
    coppie_mese = []
    for m in sorted(s["per_mese"]):
        anno, mese = m.split("-")
        coppie_mese.append((f"{mesi_it.get(mese, mese)} {anno}", s["per_mese"][m]))
    barre_mese = barre_html(coppie_mese, colore="#f0b429", mostra_pct=False)

    # Andamento cumulato (grafico a linea)
    punti_cum = []
    for m, tot_run in s["cumulato"]:
        anno, mese = m.split("-")
        punti_cum.append((f"{mesi_it.get(mese, mese)} {anno[2:]}", tot_run))
    grafico_cum = linea_svg(punti_cum)

    # Tasso di risposta per gestore
    righe_conv = ""
    for g, cont, risp, pct in s["conv_gestore"]:
        righe_conv += (
            f'<div class="brow"><div class="blbl">{html.escape(g)}</div>'
            f'<div class="btrack"><div class="bfill" style="width:{pct:.0f}%;background:#16a34a"></div></div>'
            f'<div class="bval" style="width:120px">{risp}/{cont} · {pct:.0f}%</div></div>'
        )
    barre_conv = f'<div class="barre">{righe_conv}</div>'

    # Punteggio medio per tipologia di figura
    coppie_mr = sorted(s["media_ruolo"].items(), key=lambda x: x[1], reverse=True)
    massimo_mr = 10
    righe_mr = ""
    for lbl, val in coppie_mr:
        w = val / massimo_mr * 100
        righe_mr += (
            f'<div class="brow"><div class="blbl">{html.escape(lbl)}</div>'
            f'<div class="btrack"><div class="bfill" style="width:{w:.0f}%;background:#8b5cf6"></div></div>'
            f'<div class="bval">{val:.1f}/10</div></div>'
        )
    barre_mr = f'<div class="barre">{righe_mr}</div>'

    # Tipo profilo A/B
    n_a = s["per_tipo"].get("A", 0)
    n_b = s["per_tipo"].get("B", 0)
    pct_a = n_a / s["tot"] * 100 if s["tot"] else 0

    # Sintesi esecutiva (client-ready)
    top_ruolo = s["per_ruolo"].most_common(1)[0][0] if s["per_ruolo"] else "—"
    primo_mese = punti_cum[0][0] if punti_cum else ""
    sintesi = (
        f"Nel periodo di attività sono stati individuati e profilati <b>{s['tot']} professionisti</b> "
        f"del settore private banking e consulenza finanziaria, di cui <b>{s['contattati']} contattati</b> "
        f"e <b>{s['risposte']} con risposta positiva</b> (tasso di risposta <b>{s['tasso_risposta']:.0f}%</b>). "
        f"La tipologia più rappresentata è <b>{top_ruolo}</b>, con un punteggio medio di qualità dei profili "
        f"pari a <b>{s['media']:.1f}/10</b>."
    )

    doc = f"""<!DOCTYPE html>
<html lang="it">
<head>
<meta charset="utf-8">
<title>SABIA — Cruscotto Contatti</title>
<style>
  :root {{ --blu:#1A2E4A; --azzurro:#2E7CF6; --bg:#f5f7fa; --bordo:#e2e8f0; --testo:#1e293b; --muted:#64748b; }}
  * {{ box-sizing:border-box; margin:0; padding:0; }}
  body {{ font-family:-apple-system,"Segoe UI",Roboto,sans-serif; color:var(--testo); background:var(--bg); line-height:1.5; padding:36px 22px; }}
  .wrap {{ max-width:1000px; margin:0 auto; }}
  header {{ background:linear-gradient(135deg,var(--blu),#24406b); color:#fff; padding:30px 34px; border-radius:16px; display:flex; justify-content:space-between; align-items:flex-end; }}
  header h1 {{ font-size:25px; letter-spacing:-.4px; }}
  header .sub {{ opacity:.75; font-size:13px; margin-top:4px; }}
  header .data {{ font-size:13px; opacity:.8; }}
  .sintesi {{ background:#fff; border:1px solid var(--bordo); border-left:4px solid var(--azzurro); border-radius:12px; padding:16px 20px; margin-top:18px; font-size:14px; color:#334155; }}
  .sintesi b {{ color:var(--blu); }}
  .kpis {{ display:grid; grid-template-columns:repeat(4,1fr); gap:14px; margin:18px 0 22px; }}
  .kpi {{ background:#fff; border:1px solid var(--bordo); border-radius:14px; padding:18px 20px; }}
  .kpi .v {{ font-size:30px; font-weight:700; color:var(--blu); letter-spacing:-1px; }}
  .kpi .l {{ font-size:11px; color:var(--muted); text-transform:uppercase; letter-spacing:.5px; margin-top:3px; }}
  .kpi.acc .v {{ color:var(--azzurro); }}
  .grid2 {{ display:grid; grid-template-columns:1fr 1fr; gap:18px; }}
  section {{ background:#fff; border:1px solid var(--bordo); border-radius:14px; padding:22px 26px; margin-bottom:18px; }}
  section h2 {{ font-size:15px; color:var(--blu); margin-bottom:16px; display:flex; align-items:center; gap:8px; }}
  section h2::before {{ content:""; width:4px; height:15px; background:var(--azzurro); border-radius:2px; }}
  .chart-row {{ display:flex; align-items:center; gap:22px; }}
  .chart-row svg {{ flex-shrink:0; }}
  .legenda {{ flex:1; }}
  .leg-row {{ display:flex; align-items:center; gap:8px; padding:4px 0; font-size:13.5px; }}
  .dot {{ width:11px; height:11px; border-radius:3px; flex-shrink:0; }}
  .leg-lbl {{ flex:1; }}
  .leg-val {{ color:var(--muted); font-weight:600; font-size:12.5px; }}
  .barre {{ display:flex; flex-direction:column; gap:7px; }}
  .brow {{ display:flex; align-items:center; gap:10px; font-size:13px; }}
  .blbl {{ width:34%; text-align:right; color:var(--testo); }}
  .btrack {{ flex:1; background:#eef2f7; border-radius:6px; height:14px; overflow:hidden; }}
  .bfill {{ height:100%; border-radius:6px; }}
  .bval {{ width:74px; color:var(--muted); font-weight:600; font-size:12px; }}
  .vuoto {{ color:var(--muted); font-size:13px; font-style:italic; }}
  .note {{ font-size:12.5px; color:var(--muted); background:#f8fafc; border-left:3px solid var(--azzurro); padding:11px 15px; border-radius:0 8px 8px 0; margin-top:14px; }}
  footer {{ text-align:center; color:var(--muted); font-size:12px; margin-top:22px; }}
  @media print {{ body {{ background:#fff; padding:0; }} section,.kpi {{ break-inside:avoid; }} .grid2 {{ break-inside:avoid; }} }}
</style>
</head>
<body>
<div class="wrap">

  <header>
    <div>
      <h1>Cruscotto Contatti — SABIA</h1>
      <div class="sub">Analisi delle figure ricercate e contattate · Banca Fideuram</div>
    </div>
    <div class="data">Generato il {oggi}</div>
  </header>

  <div class="sintesi"><b>In sintesi.</b> {sintesi}</div>

  <div class="kpis">
    <div class="kpi"><div class="v">{s['tot']}</div><div class="l">Contatti totali</div></div>
    <div class="kpi"><div class="v">{s['contattati']}</div><div class="l">Contattati</div></div>
    <div class="kpi acc"><div class="v">{s['risposte']}</div><div class="l">Risposte (msg inviato)</div></div>
    <div class="kpi acc"><div class="v">{s['tasso_risposta']:.0f}%</div><div class="l">Tasso di risposta</div></div>
  </div>

  <div class="grid2">
    <section>
      <h2>Stato nel funnel</h2>
      <div class="chart-row">{donut_stato}{legenda_stato}</div>
    </section>
    <section>
      <h2>Tipologia di figura professionale</h2>
      <div class="chart-row">{donut_ruolo}{legenda_ruolo}</div>
    </section>
  </div>

  <div class="grid2">
    <section>
      <h2>Qualità dei profili (punteggio AI)</h2>
      <div class="chart-row">{donut_qual}{legenda_qual}</div>
      <div class="note">Punteggio medio <b>{s['media']:.1f}/10</b> su {sum(s['dist_punteggio'].values())} profili valutati.</div>
    </section>
    <section>
      <h2>Distribuzione punteggio (1-10)</h2>
      {barre_pun}
    </section>
  </div>

  <section>
    <h2>Intelligence AI sui profili &mdash; su {arr['n_arr']} profili arricchiti</h2>
    <div class="grid2" style="gap:26px">
      <div>
        <h4 style="font-size:12px;color:var(--muted);text-transform:uppercase;letter-spacing:.4px;margin-bottom:9px">Propensione al cambiamento (mobilità)</h4>
        {barre_mob}
        <h4 style="font-size:12px;color:var(--muted);text-transform:uppercase;letter-spacing:.4px;margin:16px 0 9px">Pattern di carriera</h4>
        {barre_pattern}
      </div>
      <div>
        <h4 style="font-size:12px;color:var(--muted);text-transform:uppercase;letter-spacing:.4px;margin-bottom:9px">Momento di contatto ideale</h4>
        {barre_momento}
      </div>
    </div>
    <div class="note">Dimensioni stimate dall'AI in fase di arricchimento del profilo. <b>Nota:</b> l'età anagrafica
      non è tracciata dalle fonti (LinkedIn non la espone); questi indicatori — mobilità, seniority di carriera e
      timing — sono più azionabili per il recruiting rispetto all'età.</div>
  </section>

  <section>
    <h2>Andamento cumulato dei contatti</h2>
    {grafico_cum}
    <div class="note">Totale progressivo dei professionisti profilati mese dopo mese.</div>
  </section>

  <div class="grid2">
    <section>
      <h2>Efficacia per gestore (tasso di risposta)</h2>
      {barre_conv}
      <div class="note">Rapporto tra contatti che hanno risposto (messaggio inviato) e totale contattati, per gestore.</div>
    </section>
    <section>
      <h2>Qualità media per tipologia di figura</h2>
      {barre_mr}
      <div class="note">Punteggio medio AI (0-10) per categoria professionale — dove si concentra la qualità.</div>
    </section>
  </div>

  <div class="grid2">
    <section>
      <h2>Aziende di provenienza (top 10)</h2>
      {barre_az}
      <div class="note">Azienda disponibile su {s['n_con_azienda']} profili su {s['tot']}.</div>
    </section>
    <section>
      <h2>Carico e volumi</h2>
      {barre_gestore}
      <h2 style="margin-top:20px">Contatti per mese</h2>
      {barre_mese}
      <div class="note">Profili di tipo A (target primario): <b>{n_a}</b> su {s['tot']} ({pct_a:.0f}%){' · tipo B: ' + str(n_b) if n_b else ''}.</div>
    </section>
  </div>

  <footer>SABIA Recruiting Tool · Axiom Labs — cruscotto generato automaticamente · elenco nominativo completo nel file CSV</footer>
</div>
</body>
</html>"""

    path = os.path.join(OUT_DIR, "recap_contatti.html")
    with open(path, "w", encoding="utf-8") as f:
        f.write(doc)
    return path


def main():
    contatti = carica_dati()
    s = calcola_statistiche(contatti)
    p_csv = scrivi_csv(contatti)
    p_html = scrivi_html(contatti, s)
    print(f"✓ CSV  → {p_csv}  ({s['tot']} contatti)")
    print(f"✓ HTML → {p_html}")
    print(f"\nRiepilogo: {s['tot']} totali · {s['contattati']} contattati · "
          f"{s['risposte']} risposte ({s['tasso_risposta']:.0f}%) · media {s['media']:.1f}/10")
    print(f"Ruoli: {dict(s['per_ruolo'].most_common())}")
    print(f"Arricchiti: {s['arr']['n_arr']} · mobilità {dict(s['arr']['mobilita'])}")


if __name__ == "__main__":
    main()
