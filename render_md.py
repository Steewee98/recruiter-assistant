"""
Render leggero Markdown → HTML stilizzato (coerente col recap SABIA).
Non richiede librerie esterne. Supporta: titoli, tabelle, liste, grassetto,
codice inline e a blocco, citazioni, righe orizzontali.

Uso:  venv/bin/python render_md.py docs/piano_multifonte.md export/piano_multifonte.html
"""
import sys
import re
import html


def inline(t):
    t = html.escape(t)
    t = re.sub(r"`([^`]+)`", r"<code>\1</code>", t)
    t = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", t)
    t = re.sub(r"(?<!\w)\*([^*]+)\*(?!\w)", r"<em>\1</em>", t)
    return t


def render(md):
    linee = md.split("\n")
    out, i = [], 0
    n = len(linee)
    while i < n:
        r = linee[i]

        # Blocco codice
        if r.startswith("```"):
            buf = []
            i += 1
            while i < n and not linee[i].startswith("```"):
                buf.append(html.escape(linee[i]))
                i += 1
            i += 1
            out.append("<pre><code>" + "\n".join(buf) + "</code></pre>")
            continue

        # Riga orizzontale
        if r.strip() == "---":
            out.append("<hr>")
            i += 1
            continue

        # Titoli
        m = re.match(r"^(#{1,4})\s+(.*)", r)
        if m:
            lv = len(m.group(1))
            out.append(f"<h{lv}>{inline(m.group(2))}</h{lv}>")
            i += 1
            continue

        # Tabella (riga con | e riga separatrice sotto)
        if "|" in r and i + 1 < n and re.match(r"^\s*\|?[\s:|-]+\|[\s:|-]+", linee[i + 1]):
            header = [c.strip() for c in r.strip().strip("|").split("|")]
            i += 2
            righe = []
            while i < n and "|" in linee[i] and linee[i].strip():
                righe.append([c.strip() for c in linee[i].strip().strip("|").split("|")])
                i += 1
            th = "".join(f"<th>{inline(c)}</th>" for c in header)
            trs = ""
            for rg in righe:
                tds = "".join(f"<td>{inline(c)}</td>" for c in rg)
                trs += f"<tr>{tds}</tr>"
            out.append(f"<table><thead><tr>{th}</tr></thead><tbody>{trs}</tbody></table>")
            continue

        # Citazione
        if r.startswith(">"):
            buf = []
            while i < n and linee[i].startswith(">"):
                buf.append(inline(linee[i].lstrip(">").strip()))
                i += 1
            out.append("<blockquote>" + "<br>".join(buf) + "</blockquote>")
            continue

        # Liste (numerate o puntate)
        if re.match(r"^\s*[-*]\s+", r) or re.match(r"^\s*\d+\.\s+", r):
            ordinata = bool(re.match(r"^\s*\d+\.\s+", r))
            tag = "ol" if ordinata else "ul"
            buf = []
            while i < n and (re.match(r"^\s*[-*]\s+", linee[i]) or re.match(r"^\s*\d+\.\s+", linee[i])):
                testo = re.sub(r"^\s*(?:[-*]|\d+\.)\s+", "", linee[i])
                buf.append(f"<li>{inline(testo)}</li>")
                i += 1
            out.append(f"<{tag}>" + "".join(buf) + f"</{tag}>")
            continue

        # Paragrafo / vuoto
        if r.strip():
            out.append(f"<p>{inline(r)}</p>")
        i += 1

    return "\n".join(out)


CSS = """
:root { --blu:#1A2E4A; --azzurro:#2E7CF6; --bg:#f5f7fa; --bordo:#e2e8f0; --testo:#1e293b; --muted:#64748b; }
* { box-sizing:border-box; }
body { font-family:-apple-system,"Segoe UI",Roboto,sans-serif; color:var(--testo); background:var(--bg);
       line-height:1.6; max-width:920px; margin:0 auto; padding:48px 28px; }
h1 { font-size:28px; color:var(--blu); letter-spacing:-.5px; border-bottom:3px solid var(--azzurro);
     padding-bottom:12px; margin:0 0 20px; }
h2 { font-size:20px; color:var(--blu); margin:34px 0 12px; display:flex; align-items:center; gap:9px; }
h2::before { content:""; width:5px; height:20px; background:var(--azzurro); border-radius:2px; }
h3 { font-size:16px; color:#24406b; margin:24px 0 8px; }
h4 { font-size:14px; color:var(--muted); text-transform:uppercase; letter-spacing:.5px; margin:18px 0 6px; }
p { margin:10px 0; }
a { color:var(--azzurro); }
strong { color:var(--blu); }
hr { border:0; border-top:1px solid var(--bordo); margin:28px 0; }
ul,ol { margin:10px 0 10px 22px; }
li { margin:5px 0; }
code { background:#eef2f7; color:#1e40af; padding:2px 6px; border-radius:5px; font-size:13px;
       font-family:"SF Mono",Menlo,monospace; }
pre { background:var(--blu); color:#e2e8f0; padding:18px 20px; border-radius:12px; overflow-x:auto; }
pre code { background:none; color:#cfe0ff; padding:0; font-size:12.5px; line-height:1.5; }
table { width:100%; border-collapse:collapse; margin:16px 0; font-size:13.5px; background:#fff;
        border:1px solid var(--bordo); border-radius:10px; overflow:hidden; }
th { background:#f1f5f9; text-align:left; color:var(--blu); font-size:12px; text-transform:uppercase;
     letter-spacing:.3px; padding:10px 12px; border-bottom:2px solid var(--bordo); }
td { padding:9px 12px; border-bottom:1px solid #f1f5f9; vertical-align:top; }
tr:last-child td { border-bottom:none; }
blockquote { background:#fff8e6; border-left:4px solid #f0b429; padding:12px 18px; margin:16px 0;
             border-radius:0 8px 8px 0; color:#5b4708; }
@media print { body { background:#fff; } table,pre,blockquote { break-inside:avoid; } }
"""


def main():
    src, dst = sys.argv[1], sys.argv[2]
    with open(src, encoding="utf-8") as f:
        body = render(f.read())
    doc = f"<!DOCTYPE html><html lang='it'><head><meta charset='utf-8'><title>Piano SABIA</title><style>{CSS}</style></head><body>{body}</body></html>"
    with open(dst, "w", encoding="utf-8") as f:
        f.write(doc)
    print(f"✓ HTML → {dst}")


if __name__ == "__main__":
    main()
