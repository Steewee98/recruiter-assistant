"""
Dashboard principale — riepilogo statistiche, grafici e export PDF.
"""

import json
import io
from datetime import datetime

from flask import Blueprint, render_template, jsonify, request, Response

from database import get_db

dashboard_bp = Blueprint("dashboard", __name__)

STATI = [
    "Da valutare",
    "Da contattare",
    "Richiesta Inviata",
    "Messaggio Inviato",
    "Chiuso",
]

# Sottocategorie raggruppate sotto "Contattati" nella dashboard
STATI_CONTATTATI = ["Richiesta Inviata", "Messaggio Inviato", "Contattati"]

GESTORI = ["Salvatore Sabia", "Firdaous Filahi", "Non assegnato"]


# ─────────────────────────────────────────────
# Helper
# ─────────────────────────────────────────────

def _get_stats():
    db = get_db()

    totale = db.execute("SELECT COUNT(*) AS n FROM candidati").fetchone()["n"] or 0

    per_stato = {}
    for s in STATI + ["Contattati"]:
        row = db.execute("SELECT COUNT(*) AS n FROM candidati WHERE stato=?", (s,)).fetchone()
        per_stato[s] = row["n"] if row else 0

    # Conteggio aggregato "Contattati" (somma delle sottocategorie + valore legacy)
    per_stato["Contattati"] = sum(per_stato.get(s, 0) for s in STATI_CONTATTATI)

    per_profilo = {}
    for p in ["A", "B"]:
        row = db.execute("SELECT COUNT(*) AS n FROM candidati WHERE tipo_profilo=?", (p,)).fetchone()
        per_profilo[p] = row["n"] if row else 0

    per_gestore = {}
    for g in GESTORI:
        row = db.execute("SELECT COUNT(*) AS n FROM candidati WHERE gestore=?", (g,)).fetchone()
        per_gestore[g] = row["n"] if row else 0

    avg_row = db.execute(
        "SELECT ROUND(AVG(punteggio::numeric), 1) AS avg FROM candidati WHERE punteggio IS NOT NULL"
    ).fetchone()
    punteggio_medio = float(avg_row["avg"]) if avg_row and avg_row["avg"] else None

    ultimi = [dict(r) for r in db.execute(
        """SELECT id, nome, cognome, ruolo_attuale, stato, punteggio,
                  tipo_profilo, gestore, data_inserimento
           FROM candidati ORDER BY data_inserimento DESC LIMIT 5"""
    ).fetchall()]

    prossimi = [dict(r) for r in db.execute(
        """SELECT a.id, a.tipo, a.data_ora, a.gestore, a.stato,
                  COALESCE(c.nome || ' ' || c.cognome, 'Candidato eliminato') AS candidato_nome
           FROM appuntamenti a
           LEFT JOIN candidati c ON a.candidato_id = c.id
           WHERE a.stato = 'Da fare' AND a.data_ora >= CURRENT_TIMESTAMP
           ORDER BY a.data_ora ASC LIMIT 3"""
    ).fetchall()]

    ultime_ricerche = []
    for r in db.execute(
        """SELECT id, tipo_profilo, parametri, profili_trovati,
                  profili_importati, stato, data_ricerca
           FROM ricerche_automatiche ORDER BY data_ricerca DESC LIMIT 3"""
    ).fetchall():
        row = dict(r)
        try:
            row["parametri"] = json.loads(row["parametri"] or "{}")
        except Exception:
            row["parametri"] = {}
        ultime_ricerche.append(row)

    tot_ricerche = db.execute("SELECT COUNT(*) AS n FROM ricerche_automatiche").fetchone()["n"] or 0

    db.close()

    return {
        "totale": totale,
        "per_stato": per_stato,
        "per_profilo": per_profilo,
        "per_gestore": per_gestore,
        "punteggio_medio": punteggio_medio,
        "tot_ricerche": tot_ricerche,
        "ultimi_candidati": ultimi,
        "prossimi_appuntamenti": prossimi,
        "ultime_ricerche": ultime_ricerche,
        "aggiornato_alle": datetime.now().strftime("%H:%M:%S"),
    }


# ─────────────────────────────────────────────
# Routes
# ─────────────────────────────────────────────

@dashboard_bp.route("/dashboard")
def index():
    stats = _get_stats()
    return render_template("dashboard.html", stats=stats)


@dashboard_bp.route("/dashboard/stats")
def stats():
    return jsonify(_get_stats())


@dashboard_bp.route("/dashboard/candidati/<path:stato>")
def candidati_per_stato(stato):
    """Restituisce in JSON i candidati di uno stato, ordinati per punteggio desc."""
    # "Contattati" e' un raggruppamento visivo di Richiesta Inviata + Messaggio Inviato
    if stato == "Contattati":
        db = get_db()
        risultato = {"stato": "Contattati", "sottocategorie": {}}
        totale = 0
        # Solo le due sottocategorie visibili all'utente
        for sub in ["Richiesta Inviata", "Messaggio Inviato"]:
            # Includi anche i record legacy con stato='Contattati' nella prima sottocategoria
            if sub == "Richiesta Inviata":
                rows = db.execute(
                    """SELECT id, nome, cognome, ruolo_attuale, azienda,
                              tipo_profilo, punteggio, gestore, stato,
                              analisi, spunti, messaggio_outreach, data_inserimento
                       FROM candidati
                       WHERE stato IN (?, 'Contattati')
                       ORDER BY punteggio DESC NULLS LAST, data_inserimento DESC""",
                    (sub,),
                ).fetchall()
            else:
                rows = db.execute(
                    """SELECT id, nome, cognome, ruolo_attuale, azienda,
                              tipo_profilo, punteggio, gestore, stato,
                              analisi, spunti, messaggio_outreach, data_inserimento
                       FROM candidati
                       WHERE stato = ?
                       ORDER BY punteggio DESC NULLS LAST, data_inserimento DESC""",
                    (sub,),
                ).fetchall()
            candidati_sub = [dict(r) for r in rows]
            risultato["sottocategorie"][sub] = candidati_sub
            totale += len(candidati_sub)
        db.close()
        risultato["totale"] = totale
        return jsonify(risultato)

    if stato not in STATI:
        return jsonify({"errore": "Stato non valido"}), 400

    db = get_db()
    rows = db.execute(
        """SELECT id, nome, cognome, ruolo_attuale, azienda,
                  tipo_profilo, punteggio, gestore, stato,
                  analisi, spunti, messaggio_outreach, data_inserimento
           FROM candidati
           WHERE stato = ?
           ORDER BY punteggio DESC NULLS LAST, data_inserimento DESC""",
        (stato,),
    ).fetchall()
    db.close()

    return jsonify({
        "stato": stato,
        "totale": len(rows),
        "candidati": [dict(r) for r in rows],
    })


@dashboard_bp.route("/dashboard/candidati/<int:candidato_id>/stato", methods=["PATCH"])
def aggiorna_stato_dashboard(candidato_id):
    """Aggiorna lo stato di un candidato dalla dashboard."""
    dati = request.get_json()
    nuovo_stato = dati.get("stato")

    if nuovo_stato not in STATI:
        return jsonify({"errore": "Stato non valido"}), 400

    db = get_db()
    db.execute(
        "UPDATE candidati SET stato = ?, data_aggiornamento = CURRENT_TIMESTAMP WHERE id = ?",
        (nuovo_stato, candidato_id),
    )
    db.commit()
    db.close()

    return jsonify({"successo": True, "stato": nuovo_stato})


@dashboard_bp.route("/dashboard/report_pdf")
def report_pdf():
    """Genera il report PDF completo con ReportLab."""
    try:
        from reportlab.lib.pagesizes import A4
        from reportlab.lib import colors
        from reportlab.lib.units import cm
        from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
        from reportlab.platypus import (
            SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle,
            HRFlowable, PageBreak,
        )
        from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT
    except ImportError:
        return jsonify({"errore": "ReportLab non installato. Aggiungi 'reportlab' a requirements.txt."}), 500

    db = get_db()

    # ── Dati ──────────────────────────────────────────────────────────────────
    tutti_candidati = [dict(r) for r in db.execute(
        """SELECT nome, cognome, ruolo_attuale, tipo_profilo, punteggio,
                  stato, gestore, data_inserimento, data_aggiornamento
           FROM candidati ORDER BY stato, punteggio DESC NULLS LAST"""
    ).fetchall()]

    tutte_ricerche = [dict(r) for r in db.execute(
        """SELECT tipo_profilo, parametri, profili_trovati, profili_importati,
                  stato, data_ricerca
           FROM ricerche_automatiche ORDER BY data_ricerca DESC"""
    ).fetchall()]

    db.close()

    totale = len(tutti_candidati)
    per_stato = {s: sum(1 for c in tutti_candidati if c["stato"] == s) for s in STATI}
    per_stato["Contattati"] = sum(per_stato.get(s, 0) for s in STATI_CONTATTATI)
    per_profilo = {p: sum(1 for c in tutti_candidati if c["tipo_profilo"] == p) for p in ["A", "B"]}
    per_gestore = {g: sum(1 for c in tutti_candidati if c["gestore"] == g) for g in GESTORI}
    valutati = [c for c in tutti_candidati if c["punteggio"]]
    punteggio_medio = round(sum(c["punteggio"] for c in valutati) / len(valutati), 1) if valutati else None

    # ── Colori ────────────────────────────────────────────────────────────────
    BLU    = colors.HexColor("#1A2E4A")
    AZZURRO = colors.HexColor("#2E7CF6")
    GRIGIO = colors.HexColor("#F4F6FA")
    BORDER = colors.HexColor("#DDE3EE")
    BIANCO = colors.white

    # ── Stili ─────────────────────────────────────────────────────────────────
    styles = getSampleStyleSheet()
    s_title = ParagraphStyle("title", fontSize=22, textColor=BLU, fontName="Helvetica-Bold",
                              spaceAfter=4)
    s_sub   = ParagraphStyle("sub",   fontSize=10, textColor=colors.HexColor("#6B7A99"),
                              spaceAfter=16)
    s_h2    = ParagraphStyle("h2",    fontSize=14, textColor=BLU, fontName="Helvetica-Bold",
                              spaceBefore=18, spaceAfter=6)
    s_h3    = ParagraphStyle("h3",    fontSize=11, textColor=AZZURRO, fontName="Helvetica-Bold",
                              spaceBefore=12, spaceAfter=4)
    s_body  = ParagraphStyle("body",  fontSize=9,  textColor=colors.HexColor("#374151"),
                              leading=14)
    s_small = ParagraphStyle("small", fontSize=8,  textColor=colors.HexColor("#6B7A99"))
    s_footer = ParagraphStyle("footer", fontSize=8, textColor=colors.HexColor("#9CA3AF"),
                               alignment=TA_CENTER)

    # ── Header/footer callback ────────────────────────────────────────────────
    oggi = datetime.now().strftime("%d/%m/%Y %H:%M")

    def _on_page(canvas, doc):
        canvas.saveState()
        # Header
        canvas.setFillColor(BLU)
        canvas.rect(0, A4[1] - 1.4*cm, A4[0], 1.4*cm, fill=1, stroke=0)
        canvas.setFillColor(BIANCO)
        canvas.setFont("Helvetica-Bold", 11)
        canvas.drawString(1.5*cm, A4[1] - 0.9*cm, "SABIA Recruiting Tool")
        canvas.setFont("Helvetica", 9)
        canvas.drawRightString(A4[0] - 1.5*cm, A4[1] - 0.9*cm, f"Report del {oggi}")
        # Footer
        canvas.setFillColor(BORDER)
        canvas.rect(0, 0, A4[0], 1*cm, fill=1, stroke=0)
        canvas.setFillColor(colors.HexColor("#6B7A99"))
        canvas.setFont("Helvetica", 8)
        canvas.drawString(1.5*cm, 0.35*cm, "SABIA — Uso riservato")
        canvas.drawRightString(A4[0] - 1.5*cm, 0.35*cm, f"Pag. {doc.page}")
        canvas.restoreState()

    # ── Documento ─────────────────────────────────────────────────────────────
    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=A4,
        leftMargin=1.8*cm, rightMargin=1.8*cm,
        topMargin=2.2*cm, bottomMargin=1.8*cm,
        title="SABIA Report", author="SABIA Recruiting Tool",
    )

    story = []

    # ── SEZIONE 1 — Riepilogo numerico ────────────────────────────────────────
    story.append(Paragraph("Report Candidati", s_title))
    story.append(Paragraph(f"Generato il {oggi} — Totale candidati: <b>{totale}</b>", s_sub))
    story.append(HRFlowable(width="100%", thickness=1, color=AZZURRO, spaceAfter=12))

    story.append(Paragraph("1. Riepilogo numerico", s_h2))

    # Tabella stati
    stato_data = [["Stato", "Candidati", "% sul totale"]]
    for s in STATI:
        n = per_stato.get(s, 0)
        pct = f"{n/totale*100:.1f}%" if totale else "0%"
        stato_data.append([s, str(n), pct])
    story.append(_table(stato_data, AZZURRO, GRIGIO, BLU))
    story.append(Spacer(1, 10))

    # Tabella profili + gestori affiancate
    row1 = [["Profilo", "N."], ["A", str(per_profilo.get("A", 0))], ["B", str(per_profilo.get("B", 0))]]
    row2 = [["Gestore", "N."]]
    for g in GESTORI:
        row2.append([g, str(per_gestore.get(g, 0))])

    t_left  = _table(row1,  AZZURRO, GRIGIO, BLU, col_widths=[5*cm, 2.5*cm])
    t_right = _table(row2,  AZZURRO, GRIGIO, BLU, col_widths=[6.5*cm, 2.5*cm])
    combo = Table([[t_left, Spacer(1, 1), t_right]], colWidths=[7.5*cm, 0.5*cm, 9*cm])
    combo.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP")]))
    story.append(combo)
    story.append(Spacer(1, 8))

    # KPI sintetici
    kpi_data = [
        ["Totale ricerche lanciate", str(len(tutte_ricerche))],
        ["Punteggio medio candidati valutati", f"{punteggio_medio}/100" if punteggio_medio else "—"],
        ["Candidati con punteggio", str(len(valutati))],
    ]
    story.append(_table(kpi_data, AZZURRO, GRIGIO, BLU, header=False))

    story.append(PageBreak())

    # ── SEZIONE 2 — Lista candidati completa ──────────────────────────────────
    story.append(Paragraph("2. Lista candidati completa", s_h2))

    cand_data = [["Nome", "Ruolo", "Profilo", "Punt.", "Stato", "Gestore", "Inserito"]]
    for c in tutti_candidati:
        nome  = f"{c['nome'] or ''} {c['cognome'] or ''}".strip() or "—"
        ruolo = (c["ruolo_attuale"] or "—")[:30]
        ins   = (c["data_inserimento"] or "")[:10]
        punt  = str(c["punteggio"]) if c["punteggio"] else "—"
        cand_data.append([nome, ruolo, c["tipo_profilo"] or "—", punt,
                          c["stato"] or "—", c["gestore"] or "—", ins])

    story.append(_table(
        cand_data, AZZURRO, GRIGIO, BLU,
        col_widths=[3.8*cm, 4*cm, 1.4*cm, 1.2*cm, 3.2*cm, 3.4*cm, 2*cm],
        font_size=7.5,
    ))

    story.append(PageBreak())

    # ── SEZIONE 3 — Candidati per stato ───────────────────────────────────────
    story.append(Paragraph("3. Candidati per stato", s_h2))

    for stato in STATI:
        gruppo = [c for c in tutti_candidati if c["stato"] == stato]
        if not gruppo:
            continue
        story.append(Paragraph(f"{stato} ({len(gruppo)})", s_h3))
        g_data = [["Nome", "Ruolo", "Punteggio"]]
        for c in gruppo:
            nome  = f"{c['nome'] or ''} {c['cognome'] or ''}".strip() or "—"
            ruolo = (c["ruolo_attuale"] or "—")[:45]
            punt  = str(c["punteggio"]) if c["punteggio"] else "—"
            g_data.append([nome, ruolo, punt])
        story.append(_table(g_data, AZZURRO, GRIGIO, BLU,
                             col_widths=[5*cm, 9*cm, 3*cm], font_size=8))
        story.append(Spacer(1, 6))

    story.append(PageBreak())

    # ── SEZIONE 4 — Ricerche effettuate ───────────────────────────────────────
    story.append(Paragraph("4. Ricerche effettuate", s_h2))

    r_data = [["Data", "Profilo", "Ricerca", "Trovati", "Importati", "Stato"]]
    for r in tutte_ricerche:
        data = (r["data_ricerca"] or "")[:10]
        try:
            params = json.loads(r["parametri"] or "{}")
            query  = params.get("ruolo") or params.get("keywords") or "—"
        except Exception:
            query = "—"
        r_data.append([
            data, r["tipo_profilo"] or "—", str(query)[:35],
            str(r["profili_trovati"] or 0), str(r["profili_importati"] or 0),
            r["stato"] or "—",
        ])

    story.append(_table(r_data, AZZURRO, GRIGIO, BLU,
                         col_widths=[2.2*cm, 1.6*cm, 7.5*cm, 1.8*cm, 2*cm, 2.8*cm],
                         font_size=8))

    # ── Build ─────────────────────────────────────────────────────────────────
    doc.build(story, onFirstPage=_on_page, onLaterPages=_on_page)
    buf.seek(0)

    filename = f"sabia_report_{datetime.now().strftime('%Y%m%d_%H%M')}.pdf"
    return Response(
        buf.read(),
        mimetype="application/pdf",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


def _table(data, header_bg, row_bg, text_color, col_widths=None, font_size=9, header=True):
    """Helper: crea una Table ReportLab con stile SABIA."""
    from reportlab.platypus import Table, TableStyle
    from reportlab.lib import colors
    from reportlab.lib.units import cm

    AZZURRO = colors.HexColor("#2E7CF6")
    BIANCO  = colors.white

    t = Table(data, colWidths=col_widths, repeatRows=1 if header else 0)
    style = [
        ("FONTNAME",   (0, 0), (-1, -1),  "Helvetica"),
        ("FONTSIZE",   (0, 0), (-1, -1),  font_size),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [BIANCO, colors.HexColor("#F4F6FA")]),
        ("GRID",       (0, 0), (-1, -1),  0.4, colors.HexColor("#DDE3EE")),
        ("VALIGN",     (0, 0), (-1, -1),  "MIDDLE"),
        ("LEFTPADDING",  (0, 0), (-1, -1), 6),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING",   (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING",(0, 0), (-1, -1), 4),
    ]
    if header:
        style += [
            ("BACKGROUND",  (0, 0), (-1, 0), header_bg),
            ("TEXTCOLOR",   (0, 0), (-1, 0), BIANCO),
            ("FONTNAME",    (0, 0), (-1, 0), "Helvetica-Bold"),
            ("FONTSIZE",    (0, 0), (-1, 0), font_size + 0.5),
        ]
    t.setStyle(TableStyle(style))
    return t
