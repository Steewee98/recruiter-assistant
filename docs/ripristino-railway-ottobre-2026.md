# Ripristino dopo l'incidente Railway del 21-22 ottobre 2026

Railway ha subito una violazione di sicurezza il 21 ottobre e ha raccomandato di
revocare tutte le variabili d'ambiente conservate sulla piattaforma. I build sono
stati sospesi e riaperti a scaglioni: Enterprise, Pro, Hobby, Free. Questo
progetto è su Hobby.

## Stato al 22 ottobre, ore 12

- L'app online funziona, ma è ferma al commit `3543483` (20 ottobre, 18:21).
- Due commit non sono mai stati pubblicati: i dieci post in bacheca e `/versione`.
- Tutti i commit sono su GitHub: nessun lavoro è a rischio.
- Database integro: 308 candidati, tutte le tabelle presenti.
- Nessun segno di manomissione, ma una lettura non lascia traccia: l'assenza di
  segni non è una prova.

## Cosa deve fare Stefano (non automatizzabile)

1. **Controllare i consumi** su Anthropic, Apify ed EnrichLayer. Se non c'è
   traffico anomalo, l'urgenza della rotazione scende molto.
2. **Controllare la posta**: Railway contatta direttamente i clienti i cui
   account risultano coinvolti.
3. **Riconnettere GitHub** dentro Railway. La rotazione delle credenziali ha
   staccato il collegamento: è il motivo per cui i commit del 20 ottobre sono
   arrivati su GitHub ma non sono mai diventati un deploy. Finché non si
   riconnette, ogni pubblicazione va fatta a mano.

## Variabili da ruotare, in ordine di gravità

| Variabile | Cosa apre | Perché conta |
|---|---|---|
| `LOGIN_PASSWORD` | l'accesso al programma | dati di 308 persone reali |
| `DATABASE_URL` | il database, senza passare dal programma | stessi dati, accesso diretto |
| `ANTHROPIC_API_KEY` | credito prepagato | spesa |
| `APIFY_API_KEY` | scraping a consumo | spesa |
| `PROXYCURL_API_KEY` | arricchimento profili | spesa |

Le credenziali nuove **non entrano in funzione senza un deploy**: vanno cambiate
e pubblicate nello stesso passaggio, altrimenti l'app online resta con le vecchie
e smette di funzionare.

## Sequenza quando i build Hobby riaprono

1. Generare le chiavi nuove presso ogni fornitore.
2. Scriverle nelle variabili di Railway e nel `.env` locale.
3. Un solo deploy.
4. Verificare `/versione`: deve rispondere con la revisione nuova.
5. Verificare che l'accesso funzioni con la password nuova.
6. Riconnettere GitHub, così i deploy ripartono da soli.

## Fuori dall'incidente, ma da sistemare

Il token GitHub è in chiaro dentro `.git/config`, nell'URL del remote. Va tolto
da lì e rigenerato: essendo passato in chiaro va considerato bruciato.

## Decisione presa il 22 ottobre

Stefano ha scelto di **aspettare la riapertura dei build** e fare rotazione e
pubblicazione in un passaggio solo, invece di ruotare subito mandando offline
l'app. Il rischio dell'attesa è accettato consapevolmente.

Il segnale che ribalta la decisione: **consumi anomali** su Anthropic, Apify o
EnrichLayer. Se compaiono, si revoca immediatamente senza aspettare i build,
anche a costo di lasciare l'app ferma.

Una cautela sulla sequenza: **non scrivere le chiavi nuove dentro Railway finché
non dichiarano chiuso l'incidente.** Sarebbe mettere i segreti nuovi nello stesso
magazzino appena svaligiato, con le indagini ancora in corso. Prima la conferma,
poi le chiavi nuove, poi il deploy.
