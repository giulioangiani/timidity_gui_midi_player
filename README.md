# MIDI Player – TiMidity++

Lettore MIDI grafico in Python (Tkinter) che usa **TiMidity++** come motore di
riproduzione. Pensato per chi suona/segue spartiti, basi e karaoke: permette di
mutare singoli canali, trasporre, partire da un punto qualsiasi, vedere il testo
(lyrics/karaoke) sincronizzato e gestire una playlist.

**Licenza:** GNU GPL v3 (vedi il file [`LICENSE`](LICENSE)).

---

## Caratteristiche

- **Playlist** con aggiunta multipla, riordino (su/giù), rimozione, svuotamento,
  pulsanti precedente/successivo, **avanzamento automatico** a fine brano e
  opzione **ripeti**.
- **Salva/Carica playlist** in formato `.m3u`.
- **Ricerca libera** ricorsiva nella cartella `MIDI/`: casella veloce,
  case-insensitive e multi-parola, con popup dei risultati da cui aggiungere i
  brani alla playlist.
- **Mute dei canali**: 10 checkbox (canali MIDI 1–10) per disattivare le parti,
  con pulsanti "Muta tutto" / "Smuta tutto".
- **Trasposizione** da −24 a +24 semitoni.
- **Seek**: avvio del brano da un punto qualsiasi (in secondi o `mm:ss`).
- **Testo / karaoke**: estrae il testo dal MIDI e lo mostra a lato, evidenziando
  la riga corrente **in anticipo** (intervallo regolabile) durante la
  riproduzione.
- **Barra di avanzamento** con tempo trascorso / durata totale.
- **Riquadro di output** di timidity integrato nella finestra (niente terminale).
- **Dimensione del testo** dell'interfaccia regolabile a run-time.
- **Menubar** (File / Edit / Help) con scorciatoie da tastiera.

---

## Requisiti

- **Python 3** con **Tkinter** (pacchetto di sistema `python3-tk`).
- **TiMidity++** installato e funzionante (con un soundfont GM, es.
  `fluid-soundfont-gm`).
- **mido** (libreria Python, opzionale ma consigliata): senza di essa il
  programma funziona ma **senza** seek, durata/avanzamento ed estrazione del
  testo.

---

## Installazione

Su Debian/Ubuntu:

```bash
# motore audio + interfaccia grafica
sudo apt install timidity fluid-soundfont-gm python3-tk

# libreria Python opzionale (seek, durata, testo/karaoke)
pip install -r requirements.txt      # oppure: pip install mido
```

> Se usi un Python di conda/pyenv, ricorda che `python3-tk` di apt vale solo per
> il Python di sistema; per gli altri interpreti vedi la documentazione di
> tkinter del tuo ambiente.

---

## La cartella `MIDI/`

La funzione **Cerca** lavora su una sottocartella chiamata `MIDI/` posta
**accanto allo script** (se non la trova lì, la cerca nella cartella da cui
lanci il programma). Crea la cartella e mettici dentro i tuoi file, anche
organizzati in sottocartelle:

```
midi-player-timidity/
├── midi_player.py
├── MIDI/
│   ├── Natale/Astro del Ciel.mid
│   ├── Liturgia/Alleluia.kar
│   └── ...
└── ...
```

La ricerca trova ricorsivamente i file `.mid`, `.midi` e `.kar`. La cartella
`MIDI/` è esclusa dal versionamento in `.gitignore` (i brani non vengono
caricati sul repo): rimuovi quella riga se invece vuoi versionarli.

---

## Avvio

```bash
python3 midi_player.py
```

---

## Guida all'uso

- **Aggiungere brani**: `File ▸ Aggiungi file…` (o il pulsante "Aggiungi…" nel
  pannello Playlist) per scegliere uno o più file; oppure `File ▸ Cerca…` per
  cercarli nella cartella `MIDI/`. Doppio clic su un risultato lo aggiunge alla
  coda.
- **Riprodurre**: seleziona un brano e premi **▶ Suona**; **■ Ferma** lo arresta.
  ⏮ / ⏭ passano al brano precedente/successivo.
- **Mutare canali**: spunta i numeri (1–10) dei canali da silenziare. Il canale
  10 è di solito la batteria. Le spunte valgono al successivo avvio.
- **Trasporre**: imposta i semitoni nello spinbox "Trasporta".
- **Partire da un punto**: scrivi i secondi (`90`) o `mm:ss` (`1:30`) nel campo
  "Parti da" e premi ▶. (Richiede mido.)
- **Testo/karaoke**: scegliendo un brano, il testo compare nel pannello centrale;
  in riproduzione la riga corrente si evidenzia. Regola "Anticipo (s)" per farla
  illuminare un po' prima.
- **Avanzamento automatico / Ripeti**: dal pannello Playlist o dal menu `Edit`.
- **Dimensione testo**: spinbox in alto a destra, modificabile a run-time.

---

## Come funziona (note tecniche)

- **Mute dei canali** → opzione `-Q` di timidity (`-Q 2,3,10` muta quei canali).
  timidity muta per *canale MIDI* (1–16), non per "traccia" del file: nella
  maggior parte dei MIDI le due cose coincidono.
- **Trasposizione** → opzione `-K n` di timidity (`--adjust-key`, da −24 a +24).
- **Seek** → timidity non ha un'opzione per partire da un punto, quindi il file
  MIDI viene "ritagliato" al volo con **mido**: gli eventi di stato precedenti
  (program change, volumi, tempo, pitch) vengono mantenuti all'inizio così gli
  strumenti suonano corretti, poi la riproduzione parte dal tick richiesto.
- **Output a finestra** → timidity viene avviato con l'interfaccia "dumb" (`-id`),
  che stampa righe pulite; lo standard output viene letto in un thread separato e
  riversato nel riquadro (Tkinter non è thread-safe, quindi si usa una coda).
- **Avanzamento** → cronometro "a parete" (`time.monotonic`) sommato al punto di
  partenza; la durata totale viene da `mido`.
- **Testo** → estrazione degli eventi `lyrics`/`text`/`marker`; gestisce sia i
  karaoke a sillabe (con separatori `/` e `\`) sia il testo riga-per-evento.

---

## Limitazioni note

- La sincronia del testo e l'avanzamento si basano su un cronometro: al primo
  avvio il caricamento del soundfont può ritardare l'audio di ~1 s, quindi il
  conteggio può risultare leggermente avanti (dalla seconda volta è allineato).
  Si può compensare abbassando "Anticipo".
- Trasposizione, mute e seek si applicano all'avvio della riproduzione: per
  cambiarli su un brano già in corso occorre premere di nuovo ▶.
- La scansione della cartella `MIDI/` avviene all'apertura del popup di ricerca:
  i file aggiunti mentre il popup è aperto compaiono solo riaprendolo.

---

## Licenza

Questo programma è software libero: puoi ridistribuirlo e/o modificarlo secondo
i termini della **GNU General Public License versione 3** pubblicata dalla Free
Software Foundation. Il programma è distribuito nella speranza che sia utile, ma
**SENZA ALCUNA GARANZIA**. Vedi il file [`LICENSE`](LICENSE) per il testo
completo, oppure <https://www.gnu.org/licenses/>.

---

## Autore

Copyright (C) 2026 *Giulio Angiani*

Il software è stato sviluppato con l'ausilio di modelli intelligenti di generazione del codice
