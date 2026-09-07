"""
Connettore OCF storico — elenchi passati recuperati dall'Internet Archive.

Perché serve: i passaggi di rete esistono solo come DIFFERENZA fra due elenchi.
Partendo da oggi bisognerebbe aspettare mesi per avere abbastanza esempi. Ma il
vecchio URL degli elenchi OCF (`/portal/documents/20151/296511/CF-ABILITATI.zip`)
è stato archiviato più volte dal Wayback Machine fra il 2022 e il 2023: da lì si
ricostruisce lo storico e si hanno migliaia di passaggi osservati subito.

⚠️ Le copie archiviate sono TRONCATE a 1 MiB dal crawler: manca la coda dello ZIP
(central directory) e le ultime regioni in ordine alfabetico. Non sono aperibili
con `zipfile`. Qui vengono recuperate scandendo i "local file header" e
decomprimendo le voci che rientrano interamente nel troncone — in pratica si
salvano ~11 regioni su 22, fra cui Lombardia e Lazio (i due mercati principali).
Le regioni mancanti vanno dichiarate, non nascoste: i tassi calcolati valgono
sulle regioni disponibili.
"""

import io
import json
import logging
import re
import struct
import zlib
from datetime import date

import requests

logger = logging.getLogger(__name__)

FONTE = "ocf_storico"

CDX = "http://web.archive.org/cdx/search/cdx"
WAYBACK = "https://web.archive.org/web/{ts}id_/{url}"
TIMEOUT = 180

# Firma di inizio di una voce ZIP
_LFH = b"PK\x03\x04"


def elenchi_archiviati(nome_file: str = "CF-ABILITATI.zip") -> list:
    """
    Interroga l'indice del Wayback Machine e restituisce le copie archiviate
    dell'elenco, ordinate per data: [{"data": date, "timestamp": str, "url": str}].
    Non solleva: se l'archivio non risponde, ritorna lista vuota.
    """
    try:
        r = requests.get(CDX, params={
            "url": "organismocf.it", "matchType": "domain", "output": "json",
            "filter": "mimetype:application/zip", "fl": "timestamp,original,length",
            "limit": 500,
        }, timeout=TIMEOUT)
        r.raise_for_status()
        righe = json.loads(r.text or "[]")
    except Exception as e:
        logger.warning("Wayback non raggiungibile: %s", e)
        return []

    fuori = []
    for riga in righe[1:]:
        try:
            ts, originale = riga[0], riga[1]
        except (IndexError, TypeError):
            continue
        if nome_file.lower() not in originale.lower():
            continue
        fuori.append({
            "timestamp": ts,
            "data": date(int(ts[0:4]), int(ts[4:6]), int(ts[6:8])),
            "url": WAYBACK.format(ts=ts, url=originale),
        })
    fuori.sort(key=lambda x: x["timestamp"])
    return fuori


def scarica(voce: dict) -> bytes:
    """Scarica una copia archiviata (può essere troncata: è previsto)."""
    r = requests.get(voce["url"], timeout=TIMEOUT)
    r.raise_for_status()
    return r.content


def csv_da_zip_troncato(dati: bytes) -> dict:
    """
    Estrae i CSV da un archivio ZIP anche se privo della coda.

    Scandisce i local file header: per ognuno legge nome e dimensione compressa,
    e decomprime solo se i byte ci sono tutti. Le voci tagliate a metà vengono
    saltate — meglio una regione in meno che righe corrotte in un dataset.
    """
    trovati = {}
    i = 0
    while True:
        i = dati.find(_LFH, i)
        if i < 0:
            break
        try:
            (_ver, _flag, metodo, _mt, _md, _crc, compressa, _originale,
             len_nome, len_extra) = struct.unpack("<HHHHHIIIHH", dati[i + 4:i + 30])
            nome = dati[i + 30:i + 30 + len_nome].decode("utf-8", "replace")
            inizio = i + 30 + len_nome + len_extra
        except Exception:
            i += 4
            continue

        if not nome.upper().endswith(".CSV") or compressa == 0 or inizio + compressa > len(dati):
            i += 4
            continue
        try:
            grezzo = dati[inizio:inizio + compressa]
            testo = (zlib.decompressobj(-15).decompress(grezzo) if metodo == 8 else grezzo)
            trovati[nome.split("/")[-1]] = testo.decode("utf-8", "replace")
            i = inizio + compressa
        except Exception:
            i += 4
    return trovati


def leggi_snapshot(dati: bytes):
    """
    Da un archivio (anche troncato) a un dizionario {chiave_persona: record},
    usando lo stesso parsing e la stessa minimizzazione dell'elenco corrente.
    Ritorna anche l'elenco delle regioni effettivamente recuperate.
    """
    from connettori.ocf_elenco import leggi_iscritti

    file_csv = csv_da_zip_troncato(dati)
    if not file_csv:
        return {}, []

    # Riconfeziona i CSV recuperati in uno ZIP valido, così il parsing resta uno solo
    buf = io.BytesIO()
    import zipfile
    with zipfile.ZipFile(buf, "w") as z:
        for nome, testo in file_csv.items():
            z.writestr(nome, testo)

    persone = {r["chiave"]: r for r in leggi_iscritti(buf.getvalue())}
    regioni = sorted(re.sub(r"_CFAB\.csv$", "", n, flags=re.I) for n in file_csv)
    return persone, regioni
