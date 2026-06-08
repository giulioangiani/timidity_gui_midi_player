#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Mini player MIDI con TiMidity++
--------------------------------
- Sceglie un file .mid con un file chooser
- 10 checkbox (canali 1..10): spunta = canale DA DISATTIVARE (muto)
- Avvia timidity in background mutando i canali spuntati (opzione -Q)
- Ferma timidity
- Avvio da un punto qualsiasi (in secondi oppure mm:ss)

Nota tecnica: timidity NON ha un'opzione per partire da un punto.
Per ottenere il seek il file MIDI viene "ritagliato" al volo con la
libreria mido (mantenendo strumenti/volumi/tempo gia' impostati) e si
suona il file temporaneo. Se mido non e' installato il seek viene
disabilitato e il brano parte sempre dall'inizio.

Dipendenze:
	sudo apt install timidity        # obbligatoria
	pip install mido                 # opzionale, solo per il seek
"""

import os
import re
import time
import bisect
import signal
import shutil
import tempfile
import subprocess
import queue
import threading
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from tkinter.scrolledtext import ScrolledText
import tkinter.font as tkfont

# ---- mido e' opzionale: serve solo per il seek -----------------------------
try:
	import mido
	MIDO_OK = True
except ImportError:
	MIDO_OK = False


# ===========================================================================
#  Ritaglio del MIDI (seek) tramite mido
# ===========================================================================
STATE_TYPES = {'program_change', 'control_change', 'pitchwheel', 'aftertouch',
			   'polytouch', 'set_tempo', 'time_signature', 'key_signature',
			   'sysex'}

POPUP_SEARCH_GEOMETRY = "900x520"
MAIN_WINDOW_SIZE = "1900x820"



def _tempo_map(mid):
	"""Lista ordinata (tick_assoluto, tempo) dei cambi di tempo."""
	events = []
	for track in mid.tracks:
		t = 0
		for msg in track:
			t += msg.time
			if msg.type == 'set_tempo':
				events.append((t, msg.tempo))
	events.sort(key=lambda x: x[0])
	return events


def _seconds_to_tick(mid, target_sec):
	"""Converte una posizione in secondi nel tick corrispondente,
	tenendo conto degli eventuali cambi di tempo."""
	tpb = mid.ticks_per_beat
	cur_tempo = 500000          # 120 BPM di default finche' non c'e' un set_tempo
	last_tick = 0
	elapsed = 0.0
	for tick, tempo in _tempo_map(mid):
		seg = mido.tick2second(tick - last_tick, tpb, cur_tempo)
		if elapsed + seg >= target_sec:
			remain = target_sec - elapsed
			return last_tick + int(mido.second2tick(remain, tpb, cur_tempo))
		elapsed += seg
		last_tick = tick
		cur_tempo = tempo
	remain = target_sec - elapsed
	return last_tick + int(mido.second2tick(remain, tpb, cur_tempo))


def trim_midi(in_path, out_path, start_sec):
	"""Crea out_path partendo da start_sec di in_path.
	Restituisce True se ha effettivamente ritagliato qualcosa."""
	mid = mido.MidiFile(in_path)
	cut = _seconds_to_tick(mid, start_sec)
	if cut <= 0:
		return False

	new = mido.MidiFile(ticks_per_beat=mid.ticks_per_beat, type=mid.type)
	for track in mid.tracks:
		# converto i delta-time in tick assoluti
		abs_track, t = [], 0
		for msg in track:
			t += msg.time
			abs_track.append((t, msg))

		# eventi "di stato" prima del taglio: li collasso a tick 0
		# cosi' strumenti, volumi, tempo e pitch sono gia' giusti
		before = [m for (tk, m) in abs_track if tk < cut and m.type in STATE_TYPES]
		after = [(tk, m) for (tk, m) in abs_track if tk >= cut]

		nt = mido.MidiTrack()
		for m in before:
			nt.append(m.copy(time=0))
		prev = cut
		for tk, m in after:
			nt.append(m.copy(time=tk - prev))
			prev = tk
		new.tracks.append(nt)

	new.save(out_path)
	return True


def parse_position(text):
	"""Accetta '90', '90.5' oppure 'mm:ss' e restituisce i secondi (float)."""
	text = text.strip()
	if not text:
		return 0.0
	if ':' in text:
		m, s = text.split(':', 1)
		return int(m) * 60 + float(s)
	return float(text)


def extract_text(path):
	"""Estrae il testo (lyrics/text/marker) dal MIDI.
	Ritorna (testo_completo, timeline) dove timeline = [(secondi, riga), ...].
	Gestisce sia i karaoke a sillabe (.kar) sia il testo riga-per-evento."""
	mid = mido.MidiFile(path)
	raw, t = [], 0.0
	for msg in mid:                      # .time in secondi (tempo applicato)
		t += msg.time
		if msg.is_meta and msg.type in ('lyrics', 'text', 'marker') and msg.text:
			raw.append((t, msg.text))
	if not raw:
		return "", []

	# se compaiono i separatori karaoke ('/', '\', a-capo) e' stile sillabe
	has_ctrl = any(re.search(r'[/\\\r\n]', txt) for _, txt in raw)
	lines = []
	if has_ctrl:
		cur, cur_sec = "", None

		def flush():
			nonlocal cur, cur_sec
			if cur.strip():
				lines.append((cur_sec or 0.0, cur.strip()))
			cur, cur_sec = "", None

		for sec, txt in raw:
			if txt.startswith('@'):      # righe informative dei .kar
				continue
			for p in re.split(r'([/\\\r\n])', txt):
				if p in ('/', '\\', '\r', '\n'):
					flush()
				elif p:
					if cur_sec is None:
						cur_sec = sec
					cur += p
		flush()
	else:
		for sec, txt in raw:
			if not txt.startswith('@') and txt.strip():
				lines.append((sec, txt.strip()))

	full = "\n".join(line for _, line in lines)
	return full, lines


# ===========================================================================
#  Interfaccia grafica
# ===========================================================================
class MidiPlayer(tk.Tk):
	def __init__(self):
		super().__init__()
		# dimensione testo regolabile a run-time
		self.ui_size = tk.IntVar(value=13)
		self.log_font = tkfont.Font(family="monospace", size=13)
		self.lyric_font = tkfont.Font(size=15)
		self._apply_font_size()
		self.title("MIDI Player – TiMidity++")
		self.resizable(True, True)
		self.geometry(MAIN_WINDOW_SIZE)

		self.midi_path = None
		self.proc = None
		self.tmp_file = None
		self.out_queue = queue.Queue()   # output di timidity dal thread lettore

		# stato per l'avanzamento (cronometro)
		self.total_sec = None            # durata totale del brano (con mido)
		self.seek_offset = 0.0           # secondi da cui si e' partiti
		self.play_start_wall = 0.0       # istante di avvio (time.monotonic)

		# stato per il testo/karaoke
		self.lyric_lines = []            # [(secondi, riga), ...]
		self.lyric_times = []            # solo i secondi (per bisect)
		self.cur_lyric = -1              # indice riga evidenziata
		self.lead = tk.DoubleVar(value=0.5)   # anticipo in secondi
		self.transpose = tk.IntVar(value=0)   # trasposizione in semitoni

		# stato della playlist
		self.playlist = []               # percorsi completi dei .mid
		self.current_index = -1          # brano corrente nella playlist
		self.autoadvance = tk.BooleanVar(value=True)   # avanza a fine brano
		self.loop = tk.BooleanVar(value=False)         # ripeti la playlist

		self.timidity = shutil.which("timidity")

		self._build_ui()
		self._build_menu()
		self.after(100, self._drain_log)  # svuota periodicamente la coda

		if not self.timidity:
			messagebox.showerror(
				"timidity mancante",
				"timidity non e' installato.\n\nInstallalo con:\n"
				"    sudo apt install timidity")


	def _build_menu(self):
		menubar = tk.Menu(self)

		# --- File ---
		m_file = tk.Menu(menubar, tearoff=0)
		m_file.add_command(label="Aggiungi file…", command=self.add_to_playlist)
		m_file.add_command(label="Cerca nella cartella MIDI…", command=self.open_search)
		m_file.add_separator()
		m_file.add_command(label="Carica playlist…", command=self.load_playlist)
		m_file.add_command(label="Salva playlist…", command=self.save_playlist)
		m_file.add_separator()
		m_file.add_command(label="Esci", command=self.on_close)
		menubar.add_cascade(label="File", menu=m_file)

		# --- Edit ---
		m_edit = tk.Menu(menubar, tearoff=0)
		m_edit.add_command(label="Rimuovi selezionato", command=self.remove_selected)
		m_edit.add_command(label="Svuota playlist", command=self.clear_playlist)
		m_edit.add_separator()
		# esempio di SOTTO-MENU
		m_canali = tk.Menu(m_edit, tearoff=0)
		m_canali.add_command(label="Muta tutto", command=self.mute_all)
		m_canali.add_command(label="Smuta tutto", command=self.unmute_all)
		m_edit.add_cascade(label="Canali", menu=m_canali)
		menubar.add_cascade(label="Edit", menu=m_edit)

		# --- Help ---
		m_help = tk.Menu(menubar, tearoff=0)
		m_help.add_command(
			label="Informazioni",
			command=lambda: messagebox.showinfo(
				"Informazioni",
				"GUI per MIDI Player – TiMidity++\nLettore MIDI con mute, seek, "
				"trasposizione, testo/karaoke e playlist."
				"\n\n\nCredits:\n Giulio Angiani + Claude.ai\n\n"
				"licenza GPL 3.0"))
		menubar.add_cascade(label="Help", menu=m_help)

		self.config(menu=menubar)

	# ---------------------------------------------------------------- UI ----
	def _build_ui(self):
		pad = dict(padx=10, pady=6)
		self.columnconfigure(0, weight=3)   # colonna controlli/log
		self.columnconfigure(1, weight=2)   # colonna testo a lato
		self.columnconfigure(2, weight=2)   # colonna playlist
		self.rowconfigure(6, weight=1)       # il log/testo si allargano

		# --- scelta file ---
		top = ttk.Frame(self)
		top.grid(row=0, column=0, sticky="we", **pad)
		ttk.Button(top, text="Aggiungi file…",
				   command=self.add_to_playlist).pack(side="left")
		self.file_lbl = ttk.Label(top, text="Nessun brano",
								  foreground="#666")
		self.file_lbl.pack(side="left", padx=10)

		# controllo dimensione testo (run-time), allineato a destra
		ttk.Spinbox(top, from_=8, to=40, width=4, textvariable=self.ui_size,
					command=self._apply_font_size).pack(side="right")
		ttk.Label(top, text="Dimensione testo:").pack(side="right", padx=(0, 6))

		# --- checkbox canali ---
		box = ttk.LabelFrame(
			self, text="Canali da DISATTIVARE (spunta = muto)")
		box.grid(row=1, column=0, sticky="we", **pad)
		self.mute_vars = {}
		for i in range(1, 11):
			var = tk.BooleanVar(value=False)
			self.mute_vars[i] = var
			r, c = divmod(i - 1, 5)
			ttk.Checkbutton(box, text=str(i), variable=var).grid(
				row=r, column=c, padx=8, pady=4, sticky="w")
		sel = ttk.Frame(box)
		sel.grid(row=2, column=0, columnspan=5, padx=8, pady=(2, 2), sticky="w")
		ttk.Button(sel, text="Muta tutto",
				   command=self.mute_all).pack(side="left")
		ttk.Button(sel, text="Smuta tutto",
				   command=self.unmute_all).pack(side="left", padx=8)
		ttk.Label(box, text="(numeri = canali MIDI 1–10; il 10 è di solito la batteria)",
				  foreground="#888").grid(row=3, column=0, columnspan=5,
										  padx=8, pady=(0, 4), sticky="w")

		# --- posizione di partenza ---
		pos = ttk.Frame(self)
		pos.grid(row=2, column=0, sticky="we", **pad)
		ttk.Label(pos, text="Parti da (sec o mm:ss):").pack(side="left")
		self.pos_entry = ttk.Entry(pos, width=10)
		self.pos_entry.insert(0, "0")
		self.pos_entry.pack(side="left", padx=8)
		if not MIDO_OK:
			self.pos_entry.configure(state="disabled")
			ttk.Label(pos, text="(installa 'mido' per il seek)",
					  foreground="#c0392b").pack(side="left")

		# trasposizione in semitoni (-24..24)
		ttk.Label(pos, text="Trasporta (semitoni):").pack(side="left", padx=(20, 0))
		ttk.Spinbox(pos, from_=-24, to=24, width=5,
					textvariable=self.transpose).pack(side="left", padx=8)

		# --- pulsanti play / stop ---
		btns = ttk.Frame(self)
		btns.grid(row=3, column=0, sticky="we", **pad)
		ttk.Button(btns, text="⏮", width=3,
				   command=self._prev_track).pack(side="left", padx=(0, 4))
		self.play_btn = ttk.Button(btns, text="▶  Suona",
								   command=self.play)
		self.play_btn.pack(side="left")
		self.stop_btn = ttk.Button(btns, text="■  Ferma",
								   command=self.stop, state="disabled")
		self.stop_btn.pack(side="left", padx=8)
		ttk.Button(btns, text="⏭", width=3,
				   command=self._next_track).pack(side="left")

		# --- avanzamento brano ---
		prog = ttk.Frame(self)
		prog.grid(row=4, column=0, sticky="we", padx=10, pady=(0, 4))
		self.progress = ttk.Progressbar(prog, orient="horizontal",
										mode="determinate", length=400)
		self.progress.pack(side="left", fill="x", expand=True)
		self.time_lbl = ttk.Label(prog, text="0:00", width=14, anchor="e")
		self.time_lbl.pack(side="left", padx=8)

		# --- stato ---
		self.status = ttk.Label(self, text="Pronto.", foreground="#2c3e50")
		self.status.grid(row=5, column=0, sticky="w", padx=10, pady=(0, 4))

		# --- riquadro output di timidity ---
		logbox = ttk.LabelFrame(self, text="Output di timidity")
		logbox.grid(row=6, column=0, sticky="we", padx=10, pady=(0, 10))
		self.log = ScrolledText(logbox, height=16, width=90, state="disabled",
								font=self.log_font, wrap="word")
		self.log.pack(fill="both", expand=True, padx=4, pady=4)
		ttk.Button(logbox, text="Pulisci log",
				   command=self.clear_log).pack(anchor="e", padx=4, pady=(0, 4))

		# --- pannello testo / karaoke (a lato) ---
		lyrbox = ttk.LabelFrame(self, text="Testo del MIDI")
		lyrbox.grid(row=0, column=1, rowspan=7, sticky="nsew",
					padx=(0, 10), pady=6)
		head = ttk.Frame(lyrbox)
		head.pack(fill="x", padx=4, pady=(4, 0))
		ttk.Label(head, text="Anticipo (s):").pack(side="left")
		ttk.Spinbox(head, from_=0.0, to=5.0, increment=0.5, width=5,
					textvariable=self.lead).pack(side="left", padx=6)
		self.lyrics = ScrolledText(lyrbox, state="disabled",
								   font=self.lyric_font, wrap="word",
								   width=34, cursor="arrow")
		self.lyrics.pack(fill="both", expand=True, padx=4, pady=4)
		# evidenziazione della riga corrente (in anticipo)
		self.lyrics.tag_configure("now", background="#fff3a0",
								  foreground="#000")

		# --- pannello playlist (a lato) ---
		plbox = ttk.LabelFrame(self, text="Playlist")
		plbox.grid(row=0, column=2, rowspan=7, sticky="nsew",
				   padx=(0, 10), pady=6)

		lst = ttk.Frame(plbox)
		lst.pack(fill="both", expand=True, padx=4, pady=4)
		sb = ttk.Scrollbar(lst, orient="vertical")
		self.pl_list = tk.Listbox(lst, activestyle="dotbox",
								  yscrollcommand=sb.set)
		sb.config(command=self.pl_list.yview)
		sb.pack(side="right", fill="y")
		self.pl_list.pack(side="left", fill="both", expand=True)
		self.pl_list.bind("<Double-Button-1>", self._on_playlist_dblclick)

		row1 = ttk.Frame(plbox)
		row1.pack(fill="x", padx=4, pady=(0, 2))
		ttk.Button(row1, text="🔍 Cerca…", command=self.open_search).pack(side="left")
		ttk.Button(row1, text="Aggiungi…", command=self.add_to_playlist).pack(side="left", padx=4)
		ttk.Button(row1, text="Rimuovi", command=self.remove_selected).pack(side="left")
		ttk.Button(row1, text="Svuota", command=self.clear_playlist).pack(side="left", padx=4)

		row2 = ttk.Frame(plbox)
		row2.pack(fill="x", padx=4, pady=(0, 2))
		ttk.Button(row2, text="▲ Su", command=self.move_up).pack(side="left")
		ttk.Button(row2, text="▼ Giù", command=self.move_down).pack(side="left", padx=4)
		ttk.Button(row2, text="Carica…", command=self.load_playlist).pack(side="left")
		ttk.Button(row2, text="Salva…", command=self.save_playlist).pack(side="left", padx=4)

		row3 = ttk.Frame(plbox)
		row3.pack(fill="x", padx=4, pady=(0, 4))
		ttk.Checkbutton(row3, text="Avanza automaticamente",
						variable=self.autoadvance).pack(side="left")
		ttk.Checkbutton(row3, text="Ripeti",
						variable=self.loop).pack(side="left", padx=8)

	# ------------------------------------------------------------ azioni ----
	# ----- playlist -----
	def add_to_playlist(self):
		paths = filedialog.askopenfilenames(
			title="Aggiungi file MIDI",
			filetypes=[("File MIDI", "*.mid *.midi *.kar"),
					   ("Tutti i file", "*.*")])
		if not paths:
			return
		was_empty = not self.playlist
		self.playlist.extend(paths)
		self._refresh_playlist()
		if was_empty:
			self._select_index(0)

	# ----- ricerca nella cartella MIDI -----
	@staticmethod
	def _midi_dir():
		"""Cartella 'MIDI': accanto allo script, altrimenti nella cartella corrente."""
		try:
			base = os.path.dirname(os.path.abspath(__file__))
		except NameError:
			base = os.getcwd()
		cand = os.path.join(base, "MIDI")
		return cand if os.path.isdir(cand) else os.path.join(os.getcwd(), "MIDI")

	def open_search(self):
		base = self._midi_dir()
		# scansione ricorsiva (una volta sola, all'apertura)
		files = []
		if os.path.isdir(base):
			for root, _, names in os.walk(base):
				for n in names:
					if n.lower().endswith((".mid", ".midi", ".kar")):
						full = os.path.join(root, n)
						files.append((os.path.relpath(full, base), full))
			files.sort(key=lambda x: x[0].lower())

		win = tk.Toplevel(self)
		win.title("Cerca brani nella cartella MIDI")
		win.geometry(POPUP_SEARCH_GEOMETRY)
		win.transient(self)

		top = ttk.Frame(win)
		top.pack(fill="x", padx=8, pady=8)
		ttk.Label(top, text="Cerca:").pack(side="left")
		q = ttk.Entry(top)
		q.pack(side="left", fill="x", expand=True, padx=6)
		q.focus_set()

		info = ttk.Label(win, text="", foreground="#666")
		info.pack(anchor="w", padx=8)

		frame = ttk.Frame(win)
		frame.pack(fill="both", expand=True, padx=8, pady=4)
		sb = ttk.Scrollbar(frame, orient="vertical")
		lb = tk.Listbox(frame, selectmode="extended", yscrollcommand=sb.set)
		sb.config(command=lb.yview)
		sb.pack(side="right", fill="y")
		lb.pack(side="left", fill="both", expand=True)

		shown = []   # percorsi completi attualmente elencati

		def refilter(*_):
			tokens = q.get().lower().split()
			lb.delete(0, "end")
			shown.clear()
			for rel, full in files:
				hay = rel.lower()
				if all(tok in hay for tok in tokens):
					lb.insert("end", rel)
					shown.append(full)
			info.configure(
				text=f"{len(shown)} risultati su {len(files)} file — {base}",
				foreground="#666")

		def add_selected(_=None):
			sel = lb.curselection()
			if not sel:
				return
			was_empty = not self.playlist
			for i in sel:
				self.playlist.append(shown[i])
			self._refresh_playlist()
			if was_empty:
				self._select_index(0)
			info.configure(text=f"Aggiunti {len(sel)} brani alla playlist.",
						   foreground="#2c7a2c")

		q.bind("<KeyRelease>", refilter)
		lb.bind("<Double-Button-1>", add_selected)

		btns = ttk.Frame(win)
		btns.pack(fill="x", padx=8, pady=8)
		ttk.Button(btns, text="Aggiungi alla playlist",
				   command=add_selected).pack(side="left")
		ttk.Button(btns, text="Chiudi", command=win.destroy).pack(side="right")

		if os.path.isdir(base):
			refilter()
		else:
			info.configure(text=f"Cartella non trovata: {base}",
						   foreground="#c0392b")

	def remove_selected(self):
		sel = self.pl_list.curselection()
		if not sel:
			return
		i = sel[0]
		del self.playlist[i]
		if i == self.current_index:
			self.current_index = -1
		elif i < self.current_index:
			self.current_index -= 1
		self._refresh_playlist()

	def clear_playlist(self):
		self.playlist = []
		self.current_index = -1
		self._refresh_playlist()

	def move_up(self):
		self._move(-1)

	def move_down(self):
		self._move(1)

	def _move(self, delta):
		sel = self.pl_list.curselection()
		if not sel:
			return
		i = sel[0]
		j = i + delta
		if not (0 <= j < len(self.playlist)):
			return
		self.playlist[i], self.playlist[j] = self.playlist[j], self.playlist[i]
		if self.current_index == i:
			self.current_index = j
		elif self.current_index == j:
			self.current_index = i
		self._refresh_playlist()
		self.pl_list.selection_clear(0, "end")
		self.pl_list.selection_set(j)

	def _refresh_playlist(self):
		self.pl_list.delete(0, "end")
		for k, p in enumerate(self.playlist):
			mark = "▶ " if k == self.current_index else "   "
			self.pl_list.insert("end", mark + os.path.basename(p))
		if 0 <= self.current_index < len(self.playlist):
			self.pl_list.selection_clear(0, "end")
			self.pl_list.selection_set(self.current_index)

	def _select_index(self, i):
		"""Imposta il brano corrente (carica file + testo), senza suonarlo."""
		if not (0 <= i < len(self.playlist)):
			self.midi_path = None
			self.file_lbl.configure(text="Nessun brano", foreground="#666")
			return
		self.current_index = i
		self.midi_path = self.playlist[i]
		self.file_lbl.configure(text=os.path.basename(self.midi_path),
								foreground="#000")
		self._load_lyrics()
		self._refresh_playlist()

	def _on_playlist_dblclick(self, event):
		sel = self.pl_list.curselection()
		if sel:
			self._select_index(sel[0])
			self.play()

	def _prev_track(self):
		if self.playlist:
			self._select_index(max(0, self.current_index - 1))
			self.play(start_override=0.0)

	def _next_track(self, auto=False):
		if not self.playlist:
			return
		nxt = self.current_index + 1
		if nxt >= len(self.playlist):
			if self.loop.get():
				nxt = 0
			else:
				return                      # fine playlist
		self._select_index(nxt)
		self.play(start_override=0.0)

	def save_playlist(self):
		if not self.playlist:
			messagebox.showinfo("Playlist", "La playlist è vuota.")
			return
		p = filedialog.asksaveasfilename(
			title="Salva playlist", defaultextension=".m3u",
			filetypes=[("Playlist M3U", "*.m3u"), ("Tutti i file", "*.*")])
		if not p:
			return
		try:
			with open(p, "w", encoding="utf-8") as f:
				f.write("\n".join(self.playlist) + "\n")
			self.status.configure(text=f"Playlist salvata: {os.path.basename(p)}")
		except Exception as e:
			messagebox.showerror("Errore salvataggio", str(e))

	def load_playlist(self):
		p = filedialog.askopenfilename(
			title="Carica playlist",
			filetypes=[("Playlist", "*.m3u *.txt"), ("Tutti i file", "*.*")])
		if not p:
			return
		try:
			with open(p, encoding="utf-8") as f:
				items = [ln.strip() for ln in f
						 if ln.strip() and not ln.startswith("#")]
		except Exception as e:
			messagebox.showerror("Errore caricamento", str(e))
			return
		missing = [i for i in items if not os.path.exists(i)]
		self.playlist = [i for i in items if os.path.exists(i)]
		self.current_index = -1
		self._refresh_playlist()
		if self.playlist:
			self._select_index(0)
		if missing:
			messagebox.showwarning(
				"File mancanti",
				f"{len(missing)} file non trovati e ignorati.")

	def _apply_font_size(self, *args):
		"""Cambia a run-time la dimensione di tutto il testo."""
		try:
			s = int(self.ui_size.get())
		except (tk.TclError, ValueError):
			return
		if s < 6:
			return
		for name in ("TkDefaultFont", "TkTextFont", "TkMenuFont"):
			tkfont.nametofont(name).configure(size=s)
		self.log_font.configure(size=s)
		if hasattr(self, "lyric_font"):
			self.lyric_font.configure(size=s + 2)

	@staticmethod
	def _fmt(sec):
		sec = max(0, int(sec))
		return f"{sec // 60}:{sec % 60:02d}"

	def _tick_progress(self):
		"""Aggiorna ogni ~250 ms i secondi trascorsi (cronometro a parete)."""
		if self.proc is None:
			return
		elapsed = self.seek_offset + (time.monotonic() - self.play_start_wall)
		if self.total_sec:
			elapsed = min(elapsed, self.total_sec)
			self.progress["value"] = elapsed / self.total_sec * 100
			self.time_lbl.configure(
				text=f"{self._fmt(elapsed)} / {self._fmt(self.total_sec)}")
		else:
			self.time_lbl.configure(text=self._fmt(elapsed))
		self._highlight_lyric(elapsed)
		self.after(250, self._tick_progress)

	# ----- testo / karaoke -----
	def _load_lyrics(self):
		"""Estrae il testo dal file scelto e popola il pannello laterale."""
		self.lyric_lines, self.lyric_times, self.cur_lyric = [], [], -1
		self.lyrics.configure(state="normal")
		self.lyrics.delete("1.0", "end")
		if not MIDO_OK or not self.midi_path:
			self.lyrics.insert("end", "(testo non disponibile)")
			self.lyrics.configure(state="disabled")
			return
		try:
			full, timeline = extract_text(self.midi_path)
		except Exception:
			full, timeline = "", []
		if timeline:
			self.lyric_lines = timeline
			self.lyric_times = [s for s, _ in timeline]
			self.lyrics.insert("end", "\n".join(t for _, t in timeline))
		else:
			self.lyrics.insert("end", "(nessun testo nel MIDI)")
		self.lyrics.configure(state="disabled")

	def _highlight_lyric(self, elapsed):
		"""Evidenzia la riga corrente, in anticipo di 'lead' secondi."""
		if not self.lyric_times:
			return
		try:
			lead = float(self.lead.get())
		except (tk.TclError, ValueError):
			lead = 0.0
		idx = bisect.bisect_right(self.lyric_times, elapsed + lead) - 1
		if idx == self.cur_lyric or idx < 0:
			return
		self.cur_lyric = idx
		self.lyrics.tag_remove("now", "1.0", "end")
		line = idx + 1
		self.lyrics.tag_add("now", f"{line}.0", f"{line}.end")
		self.lyrics.see(f"{line}.0")

	def clear_log(self):
		self.log.configure(state="normal")
		self.log.delete("1.0", "end")
		self.log.configure(state="disabled")

	def _log(self, text):
		"""Scrive nel riquadro (solo dal main thread)."""
		self.log.configure(state="normal")
		self.log.insert("end", text.replace("\r", "\n"))
		self.log.see("end")
		self.log.configure(state="disabled")

	def _reader(self, stream):
		"""Gira in un thread separato: legge l'output di timidity
		e lo mette in coda (tkinter non e' thread-safe)."""
		try:
			fd = stream.fileno()
			while True:
				chunk = os.read(fd, 1024)   # ritorna appena ci sono dati
				if not chunk:
					break
				self.out_queue.put(chunk.decode("utf-8", "replace"))
		except Exception:
			pass

	def _drain_log(self):
		"""Svuota la coda nel riquadro; richiamato dal main thread."""
		try:
			while True:
				self._log(self.out_queue.get_nowait())
		except queue.Empty:
			pass
		self.after(100, self._drain_log)

	def mute_all(self):
		for var in self.mute_vars.values():
			var.set(True)

	def unmute_all(self):
		for var in self.mute_vars.values():
			var.set(False)

	def play(self, start_override=None):
		if not self.timidity:
			messagebox.showerror("Errore", "timidity non è installato.")
			return
		if not self.midi_path:
			messagebox.showwarning("Attenzione",
								   "Aggiungi prima un file alla playlist.")
			return

		self.stop()  # ferma un'eventuale riproduzione precedente

		file_to_play = self.midi_path
		self.seek_offset = 0.0

		# --- posizione di partenza ---
		# start_override=0.0 viene usato dall'avanzamento automatico / prec-succ
		start = 0.0
		if start_override is not None:
			start = start_override
		elif MIDO_OK:
			try:
				start = parse_position(self.pos_entry.get())
			except ValueError:
				messagebox.showerror("Errore", "Posizione non valida.")
				return

		# --- seek: ritaglio del MIDI se richiesto ---
		if MIDO_OK and start > 0:
			try:
				fd, tmp = tempfile.mkstemp(suffix=".mid")
				os.close(fd)
				if trim_midi(self.midi_path, tmp, start):
					self.tmp_file = tmp
					file_to_play = tmp
					self.seek_offset = start
				else:
					os.remove(tmp)
					messagebox.showinfo(
						"Seek", "Posizione oltre la fine del brano: "
								"parto dall'inizio.")
			except Exception as e:
				messagebox.showerror("Errore seek", str(e))
				return

		# --- canali da mutare ---
		muted = [str(n) for n, v in self.mute_vars.items() if v.get()]
		# -id = interfaccia "dumb": stampa righe pulite (niente ncurses),
		# adatta a essere catturata e mostrata nel riquadro
		cmd = [self.timidity, file_to_play, "-id"]
		if muted:
			cmd += ["-Q", ",".join(muted)]
		# trasposizione (-K n = sposta di n semitoni)
		try:
			semi = int(self.transpose.get())
		except (tk.TclError, ValueError):
			semi = 0
		if semi:
			cmd += ["-K", str(semi)]

		try:
			# start_new_session=True -> processo in un suo gruppo, cosi'
			# con killpg fermo timidity ed eventuali figli.
			# stdout+stderr in una pipe da leggere in un thread.
			self.proc = subprocess.Popen(
				cmd, start_new_session=True,
				stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
		except Exception as e:
			messagebox.showerror("Errore avvio timidity", str(e))
			self._cleanup_tmp()
			return

		self._log(f"\n$ {' '.join(cmd)}\n")
		threading.Thread(target=self._reader, args=(self.proc.stdout,),
						 daemon=True).start()

		# --- avanzamento: durata totale (se mido) + avvio cronometro ---
		self.total_sec = None
		if MIDO_OK:
			try:
				self.total_sec = mido.MidiFile(self.midi_path).length
			except Exception:
				self.total_sec = None
		self.play_start_wall = time.monotonic()
		self.progress["value"] = 0
		self._tick_progress()

		muted_txt = (", ".join(muted)) if muted else "nessuno"
		self.status.configure(text=f"In riproduzione… (mutati: {muted_txt})")
		self.play_btn.configure(state="disabled")
		self.stop_btn.configure(state="normal")
		self._poll()

	def _poll(self):
		"""Controlla periodicamente se timidity ha finito da solo."""
		if self.proc is not None and self.proc.poll() is not None:
			self._finished()
			return
		if self.proc is not None:
			self.after(400, self._poll)

	def _finished(self):
		self.proc = None
		self._cleanup_tmp()
		if self.total_sec:
			self.progress["value"] = 100
			self.time_lbl.configure(
				text=f"{self._fmt(self.total_sec)} / {self._fmt(self.total_sec)}")
		self.play_btn.configure(state="normal")
		self.stop_btn.configure(state="disabled")
		self.status.configure(text="Riproduzione terminata.")
		# avanzamento automatico al brano successivo
		if self.autoadvance.get() and self.playlist:
			self._next_track(auto=True)

	def stop(self):
		if self.proc is not None and self.proc.poll() is None:
			try:
				os.killpg(os.getpgid(self.proc.pid), signal.SIGTERM)
			except Exception:
				try:
					self.proc.terminate()
				except Exception:
					pass
			try:
				self.proc.wait(timeout=2)
			except Exception:
				try:
					os.killpg(os.getpgid(self.proc.pid), signal.SIGKILL)
				except Exception:
					pass
			self.status.configure(text="Fermato.")
		self.proc = None
		self._cleanup_tmp()
		self.progress["value"] = 0
		self.time_lbl.configure(text="0:00")
		self.lyrics.tag_remove("now", "1.0", "end")
		self.cur_lyric = -1
		self.play_btn.configure(state="normal")
		self.stop_btn.configure(state="disabled")

	def _cleanup_tmp(self):
		if self.tmp_file and os.path.exists(self.tmp_file):
			try:
				os.remove(self.tmp_file)
			except Exception:
				pass
		self.tmp_file = None

	def on_close(self):
		self.stop()
		self.destroy()


if __name__ == "__main__":
	app = MidiPlayer()
	app.protocol("WM_DELETE_WINDOW", app.on_close)
	app.mainloop()
