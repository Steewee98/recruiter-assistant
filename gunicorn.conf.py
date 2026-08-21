# Configurazione gunicorn — caricata AUTOMATICAMENTE da gunicorn se presente
# nella working directory. Garantisce il worker "gthread" (a thread)
# indipendentemente dallo start command usato da Railway (Procfile o override).
#
# Perché: con il worker "sync" una singola ricerca smart lunga (piu' run Apify in
# sequenza) blocca il worker, che non risponde all'heartbeat e viene ucciso a
# --timeout → il browser mostra "errore di rete". Con "gthread" il timeout e' solo
# heartbeat e non e' legato alla durata della singola richiesta.
#
# NB: gli argomenti passati a riga di comando (es. --bind, --workers, --timeout nel
# Procfile) hanno la precedenza su questo file; worker_class/threads non sono nel
# comando attuale, quindi qui fanno fede.

import os

bind = f"0.0.0.0:{os.environ.get('PORT', '8080')}"
workers = 1
threads = 4
worker_class = "gthread"
timeout = 240
graceful_timeout = 30
keepalive = 5
