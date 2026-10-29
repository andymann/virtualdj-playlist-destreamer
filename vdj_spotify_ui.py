#!/usr/bin/env python3
"""
vdj_spotify_ui.py  —  VDJ Spotify → Lokal  (UI)

4-Panel-Ansicht:
  Playlists | Tracks | Modifiers + Run | Result

Kein Schreiben ohne explizites "Save playlist as …".
VDJ muss geschlossen sein, wenn gespeichert wird!
"""

import re
import traceback
import threading
import xml.etree.ElementTree as ET
from pathlib import Path
import tkinter as tk
from tkinter import ttk, messagebox

try:
    from mutagen import File as MutagenFile
    _MUTAGEN = True
except ImportError:
    _MUTAGEN = False

# ── Paths ──────────────────────────────────────────────────────────────────────
VDJ_BASE = Path.home() / "Library/Application Support/VirtualDJ"
VDJ_DB   = VDJ_BASE / "database.xml"
MYLISTS  = VDJ_BASE / "MyLists"
YELLOW   = "16776960"

# ── Normalisierung ─────────────────────────────────────────────────────────────
_STRIP_PARENS   = re.compile(r'\(.*?\)|\[.*?\]')
_STRIP_SUFFIXES = re.compile(
    r'\b(remastered?|single|album|radio|live|edit|version|mix|'
    r'extended|instrumental|acoustic|feat|ft|with)\b.*$', flags=re.I)
_ARTICLES       = re.compile(r'\b(the|a|an|der|die|das|les|le|la)\b', flags=re.I)
_STRIP_APOS     = re.compile(r"'\w?")
_NON_ALNUM      = re.compile(r'[^a-z0-9 ]')
_UMLAUT_MAP     = str.maketrans('äöüÄÖÜß', 'aouAOUs')


def _normalize_umlauts(s: str) -> str:
    return s.translate(_UMLAUT_MAP)


def core(s: str, opts: dict) -> str:
    s = s.lower()
    s = _normalize_umlauts(s)
    if opts.get('strip_parens',      True): s = _STRIP_PARENS.sub(' ', s)
    if opts.get('strip_suffixes',    True): s = _STRIP_SUFFIXES.sub(' ', s)
    if opts.get('strip_articles',    True): s = _ARTICLES.sub(' ', s)
    if opts.get('strip_apostrophes', True): s = _STRIP_APOS.sub('', s)
    s = _NON_ALNUM.sub(' ', s)
    return ' '.join(s.split())


_TRUNC_MIN_LEN = 20   # kürzerer Titel muss mind. so lang sein (gegen False Positives)


def drop_singles(s: str) -> str:
    return ' '.join(w for w in s.split() if len(w) > 1)


def title_match(t1: str, t2: str, opts: dict) -> bool:
    c1, c2 = core(t1, opts), core(t2, opts)
    if c1 == c2: return True
    if opts.get('collapse_spaces', True) and c1.replace(' ', '') == c2.replace(' ', ''):
        return True
    if opts.get('trailing_g', True):
        def _dg(s): return re.sub(r'g\b', '', s)
        if _dg(c1) == _dg(c2): return True
    if opts.get('drop_singles', True) and drop_singles(c1) == drop_singles(c2):
        return True
    if opts.get('truncated', True):
        short, long_ = sorted((c1, c2), key=len)
        if len(short) >= _TRUNC_MIN_LEN and long_.startswith(short):
            return True
    return False


def artist_core_set(s: str, opts: dict) -> set:
    parts = re.split(r'[/&,;×x]|\bfeat\.?\b|\bft\.?\b|\bvs\.?\b', s, flags=re.I)
    return {core(p, opts) for p in parts if core(p, opts)}


_SERVICE_ICON = {"spotify": "🎵", "tidal": "🌊"}

# ── VDJ-Hilfsfunktionen ────────────────────────────────────────────────────────
_NETSEARCH_PATH_PREFIXES = {
    "spotify": ("netsearch://sp", "netsearch://spotify"),
    "tidal":   ("netsearch://ti", "netsearch://tidal"),
}
_NETSEARCH_ID_PREFIXES = {
    "spotify": ("sp",),
    "tidal":   ("td",),
}


def get_service(song: ET.Element) -> str:
    """Returns 'spotify', 'tidal', 'streaming', or '' (local)."""
    attribs_lower = {k.lower(): v for k, v in song.attrib.items()}
    path         = attribs_lower.get("path", "").lower()
    netsearch_id = attribs_lower.get("netsearchid", "").lower()

    for name, prefixes in _NETSEARCH_PATH_PREFIXES.items():
        if any(path.startswith(px) for px in prefixes):
            return name
    if path.startswith("netsearch://"):
        return "streaming"

    if netsearch_id:
        for name, prefixes in _NETSEARCH_ID_PREFIXES.items():
            if any(netsearch_id.startswith(px) for px in prefixes):
                return name
        return "streaming"

    if path.endswith(".vdjcache") or "/cache/" in path:
        return "streaming"

    return ""


def has_local_marker(song: ET.Element) -> bool:
    path = song.get("FilePath", "")
    if "/Cache/" in path or path.endswith(".vdjcache"):
        return False
    for tags in song.findall("Tags"):
        stars   = tags.get("Stars",   "").strip()
        comment = tags.get("Comment", "").strip()
        if (stars and stars != "0") or comment:
            return True
    if _MUTAGEN and path:
        try:
            audio = MutagenFile(path, easy=True)
            if audio is not None:
                if audio.get('comment') or audio.get('rating'):
                    return True
                audio2 = MutagenFile(path, easy=False)
                if audio2 is not None:
                    for key in audio2.keys():
                        if key.startswith('COMM') and str(audio2[key]).strip():
                            return True
                    for key in audio2.keys():
                        if key.startswith('POPM'):
                            return True
        except Exception:
            pass
    return False


def get_db_meta(song: ET.Element) -> tuple:
    tags   = song.find("Tags")
    author = (tags.get("Author", "") if tags is not None else "").strip()
    title  = (tags.get("Title",  "") if tags is not None else "").strip()
    if not author or not title:
        stem = Path(song.get("FilePath", "")).stem
        if " - " in stem:
            parts = stem.split(" - ", 1)
            if not author: author = parts[0].strip()
            if not title:  title  = parts[1].strip()
    return author, title


def build_local_index(db_root: ET.Element, marked_only: bool = True) -> tuple:
    """Returns ([(artist, title, song)], Anzahl DB-Einträge ohne existierende Datei)."""
    result = []
    n_missing = 0
    for song in db_root.findall("Song"):
        filepath = song.get("FilePath", "")
        if not filepath:
            continue
        if "/Cache/" in filepath or filepath.endswith(".vdjcache"):
            continue
        # Streaming-Tracks ausschließen: FilePath "netsearch://…" oder netsearchId gesetzt
        if filepath.lower().startswith("netsearch://"):
            continue
        attribs_lower = {k.lower(): v for k, v in song.attrib.items()}
        if attribs_lower.get("netsearchid", "").strip():
            continue
        # Verwaiste DB-Einträge (Datei gelöscht/verschoben) ignorieren
        if not Path(filepath).is_file():
            n_missing += 1
            continue
        if marked_only and not has_local_marker(song):
            continue
        artist, title = get_db_meta(song)
        result.append((artist, title, song))
    return result, n_missing


def find_matches(sp_artist: str, sp_title: str,
                 local_tracks: list, opts: dict) -> tuple:
    """Returns ('exact'|'fuzzy'|None, [Kandidaten]) — alle Treffer, nicht nur den ersten."""
    sp_ac = artist_core_set(sp_artist, opts)
    sp_aw = {w for part in sp_ac for w in part.split() if len(w) >= 4}
    exact_hits, fuzzy_hits = [], []
    for (loc_artist, loc_title, song) in local_tracks:
        if not title_match(loc_title, sp_title, opts):
            continue
        loc_ac = artist_core_set(loc_artist, opts)
        if sp_ac & loc_ac:
            exact_hits.append(song)
            continue
        loc_aw = {w for part in loc_ac for w in part.split() if len(w) >= 4}
        if sp_aw & loc_aw:
            fuzzy_hits.append(song)
    if exact_hits: return 'exact', exact_hits
    if fuzzy_hits: return 'fuzzy', fuzzy_hits
    return None, []


def candidate_info(song: ET.Element) -> tuple:
    """(Datei, Ordner, Album, kbps, Stars, Plays) für den Auswahldialog."""
    path  = Path(song.get("FilePath", ""))
    tags  = song.find("Tags")
    infos = song.find("Infos")
    tg = lambda k: (tags.get(k,  "") if tags  is not None else "").strip()
    ig = lambda k: (infos.get(k, "") if infos is not None else "").strip()
    folder = str(path.parent)
    home = str(Path.home())
    if folder.startswith(home):
        folder = "~" + folder[len(home):]
    stars = tg("Stars")
    return (path.name, folder, tg("Album"), ig("Bitrate"),
            "★" * int(stars) if stars.isdigit() else "", ig("PlayCount"))


# ── Styling ────────────────────────────────────────────────────────────────────
BG        = "#1a1f2e"
BG2       = "#222840"
BG3       = "#2c3352"
FG        = "#d4d4d4"
FG_DIM    = "#777777"
SEL       = "#0e4f7a"
ACC_GREEN = "#4ec9b0"
ACC_YEL   = "#dcdcaa"
ACC_BLUE  = "#569cd6"
BORDER    = "#333333"


def _apply_style(style: ttk.Style):
    # 'clam' supports custom colors on all platforms; fall back gracefully
    available = style.theme_names()
    for theme in ('clam', 'alt', 'default'):
        if theme in available:
            style.theme_use(theme)
            break
    style.configure('.',
        background=BG, foreground=FG,
        fieldbackground=BG2, bordercolor=BORDER,
        troughcolor=BG2, selectbackground=SEL)
    style.configure('TFrame',      background=BG)
    style.configure('TLabel',      background=BG,  foreground=FG)
    style.configure('TLabelframe', background=BG,  relief='flat',
                    bordercolor=BORDER)
    style.configure('TLabelframe.Label', background=BG, foreground=FG_DIM,
                    font=('Helvetica', 11))
    style.configure('TScrollbar',  background=BG3, bordercolor=BG, troughcolor=BG2,
                    arrowcolor=FG_DIM)
    style.configure('TButton',     background=BG3, foreground=FG,
                    bordercolor=BORDER, focuscolor=BG3, padding=6)
    style.map('TButton',
        background=[('active', '#3e3e3e'), ('pressed', '#505050')],
        foreground=[('active', '#ffffff')])
    style.configure('Run.TButton',  background='#0e4f7a', foreground='#ffffff',
                    font=('Helvetica', 13, 'bold'), padding=8)
    style.map('Run.TButton',
        background=[('active', '#1565a0'), ('pressed', '#0a3a5a')])
    style.configure('Save.TButton', background='#2d5016', foreground='#a5d6a7',
                    font=('Helvetica', 12), padding=6)
    style.map('Save.TButton',
        background=[('active', '#3e6e22'), ('pressed', '#1e3a0e')])
    style.configure('TCheckbutton', background=BG, foreground=FG, focuscolor=BG)
    style.map('TCheckbutton',
        background=[('active', BG)],
        foreground=[('active', '#ffffff')])
    style.configure('Treeview', background=BG2, foreground=FG,
                    fieldbackground=BG2, rowheight=22, borderwidth=0)
    style.configure('Treeview.Heading', background=BG3, foreground=FG_DIM,
                    borderwidth=0, relief='flat')
    style.map('Treeview',
        background=[('selected', SEL)],
        foreground=[('selected', '#ffffff')])


# ── Hilfsdialog (ersetzt simpledialog — Python-3.14/macOS-kompatibel) ─────────
def _ask_string(parent, title: str, prompt: str, initial: str = ""):
    """Modaler Eingabedialog ohne ::tk::unsupported::MacWindowStyle."""
    result = [None]

    dlg = tk.Toplevel(parent)
    dlg.title(title)
    dlg.resizable(False, False)
    dlg.transient(parent)

    tk.Label(dlg, text=prompt, anchor='w').pack(padx=12, pady=(12, 4), fill='x')
    var = tk.StringVar(value=initial)
    entry = tk.Entry(dlg, textvariable=var, width=46)
    entry.pack(padx=12, pady=(0, 8), fill='x')
    entry.select_range(0, tk.END)
    entry.focus_set()

    def _ok(event=None):
        result[0] = var.get()
        dlg.destroy()

    def _cancel(event=None):
        dlg.destroy()

    btn_frame = tk.Frame(dlg)
    btn_frame.pack(padx=12, pady=(0, 12), fill='x')
    tk.Button(btn_frame, text="Abbrechen", command=_cancel, width=10).pack(side='right', padx=(4, 0))
    tk.Button(btn_frame, text="OK",        command=_ok,     width=10).pack(side='right')

    entry.bind("<Return>", _ok)
    entry.bind("<Escape>", _cancel)

    parent.update_idletasks()
    px, py = parent.winfo_rootx(), parent.winfo_rooty()
    pw, ph = parent.winfo_width(), parent.winfo_height()
    dlg.update_idletasks()
    dw, dh = dlg.winfo_reqwidth(), dlg.winfo_reqheight()
    dlg.geometry(f"+{px + (pw - dw)//2}+{py + (ph - dh)//2}")

    dlg.grab_set()
    parent.wait_window(dlg)
    return result[0]


def _ask_candidate(parent, artist: str, title: str, candidates: list,
                   current: ET.Element, progress: str = ""):
    """Modaler Dialog: einen von mehreren lokalen Kandidaten wählen.
    Rückgabe: (gewählter Song oder None, skip_all)."""
    result = [None, False]

    dlg = tk.Toplevel(parent)
    dlg.title("Lokale Datei wählen" + (f"  —  {progress}" if progress else ""))
    dlg.configure(bg=BG)
    dlg.transient(parent)
    dlg.minsize(600, 200)

    body = ttk.Frame(dlg, padding=10)
    body.pack(fill='both', expand=True)
    ttk.Label(body, text=f"{artist}  –  {title}",
              font=('Helvetica', 13, 'bold')).pack(anchor='w')
    ttk.Label(body, foreground=FG_DIM,
              text=f"{len(candidates)} lokale Kandidaten gefunden — "
                   f"welcher soll verwendet werden?").pack(anchor='w', pady=(2, 8))

    # Buttons zuerst (unten) packen, damit die Liste sie nie verdrängt
    btns = ttk.Frame(body)
    btns.pack(side='bottom', fill='x', pady=(10, 0))

    cols = ('file', 'folder', 'album', 'kbps', 'stars', 'plays')
    frame = ttk.Frame(body)
    frame.pack(fill='both', expand=True)
    frame.rowconfigure(0, weight=1)
    frame.columnconfigure(0, weight=1)
    tree = ttk.Treeview(frame, columns=cols, show='headings', selectmode='browse',
                        height=min(len(candidates), 8))
    for col, hdr, w, st in [('file', 'File', 280, True), ('folder', 'Folder', 300, True),
                            ('album', 'Album', 180, True), ('kbps', 'kbps', 50, False),
                            ('stars', 'Stars', 60, False), ('plays', 'Plays', 50, False)]:
        tree.heading(col, text=hdr)
        tree.column(col, width=w, minwidth=40, stretch=st,
                    anchor='w' if st else 'center')
    sb = ttk.Scrollbar(frame, orient=tk.VERTICAL, command=tree.yview)
    tree.configure(yscrollcommand=sb.set)
    tree.grid(row=0, column=0, sticky='nsew')
    sb.grid(row=0, column=1, sticky='ns')

    iid_to_song = {}
    for song in candidates:
        iid = tree.insert('', 'end', values=candidate_info(song))
        iid_to_song[iid] = song
        if song is current:
            tree.selection_set(iid)
            tree.focus(iid)
    if not tree.selection():
        first = tree.get_children()[0]
        tree.selection_set(first)
        tree.focus(first)

    def _ok(event=None):
        sel = tree.selection()
        if sel:
            result[0] = iid_to_song[sel[0]]
        dlg.destroy()

    def _skip(event=None):
        dlg.destroy()

    def _skip_all():
        result[1] = True
        dlg.destroy()

    ttk.Button(btns, text="Übernehmen", style='Run.TButton',
               command=_ok).pack(side='right', padx=(6, 0))
    ttk.Button(btns, text="Überspringen", command=_skip).pack(side='right')
    if progress:
        ttk.Button(btns, text="Alle überspringen",
                   command=_skip_all).pack(side='left')

    tree.bind("<Double-1>", _ok)
    dlg.bind("<Return>", _ok)
    dlg.bind("<Escape>", _skip)
    dlg.protocol("WM_DELETE_WINDOW", _skip)

    parent.update_idletasks()
    px, py = parent.winfo_rootx(), parent.winfo_rooty()
    pw, ph = parent.winfo_width(), parent.winfo_height()
    dlg.update_idletasks()
    dw, dh = dlg.winfo_width(), dlg.winfo_height()
    dlg.geometry(f"+{px + max(0, (pw - dw)//2)}+{py + max(0, (ph - dh)//2)}")

    tree.focus_set()
    dlg.grab_set()
    parent.wait_window(dlg)
    return result[0], result[1]


# ── Hauptfenster ───────────────────────────────────────────────────────────────
class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("VDJ  Spotify → Lokal")
        self.geometry("1280x720")
        self.minsize(900, 500)
        self.configure(bg=BG)
        self.option_add('*Font', ('Helvetica', 12))

        try:
            _apply_style(ttk.Style(self))
        except Exception as exc:
            print(f"[style] {exc} — continuing with default theme")

        self._db_root        = None
        self._playlist_paths = []
        self._current_songs  = []
        self._current_path   = None
        self._result_data    = []   # [(match_type, orig, local, iid)]
        self._result_candidates: dict = {}   # iid -> [Songs], nur bei >1 Kandidat
        self._result_choice:     dict = {}   # iid -> gewählter Song

        self._build_ui()
        self._load_db()
        self._load_playlists()

    # ── UI-Aufbau ──────────────────────────────────────────────────────────────
    def _build_ui(self):
        root = ttk.Frame(self, padding=8)
        root.pack(fill=tk.BOTH, expand=True)
        root.columnconfigure(0, weight=1, minsize=209)
        root.columnconfigure(1, weight=3)
        root.columnconfigure(2, weight=0, minsize=230)
        root.columnconfigure(3, weight=3)
        root.rowconfigure(0, weight=1)

        self._panel_playlists(root)
        self._panel_tracks(root)
        self._panel_modifiers(root)
        self._panel_result(root)

    def _scrolled_tree(self, parent, columns, headings, widths, stretch=None):
        frame = ttk.Frame(parent)
        frame.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)

        tree = ttk.Treeview(frame, columns=columns, show='headings',
                            selectmode='none')
        for col, hdr, w in zip(columns, headings, widths):
            tree.heading(col, text=hdr)
            do_stretch = (stretch is not None and col in stretch)
            tree.column(col, width=w, minwidth=max(20, w//2),
                        stretch=do_stretch, anchor='w')

        sb = ttk.Scrollbar(frame, orient=tk.VERTICAL, command=tree.yview)
        tree.configure(yscrollcommand=sb.set)
        tree.grid(row=0, column=0, sticky='nsew')
        sb.grid(row=0, column=1, sticky='ns')
        return frame, tree

    # Panel 1 — Playlists
    def _panel_playlists(self, root):
        lf = ttk.LabelFrame(root, text="Playlists", padding=(4, 4))
        lf.grid(row=0, column=0, sticky='nsew', padx=(6, 4))
        lf.rowconfigure(0, weight=1)
        lf.columnconfigure(0, weight=1)

        self.pl_list = tk.Listbox(
            lf, bg=BG2, fg=FG, selectbackground=SEL,
            selectforeground='#ffffff', borderwidth=0, highlightthickness=0,
            activestyle='none', font=('Helvetica', 12), relief='flat')
        sb = ttk.Scrollbar(lf, orient=tk.VERTICAL, command=self.pl_list.yview)
        self.pl_list.configure(yscrollcommand=sb.set)
        self.pl_list.grid(row=0, column=0, sticky='nsew')
        sb.grid(row=0, column=1, sticky='ns')
        self.pl_list.bind('<<ListboxSelect>>', self._on_playlist_select)

    # Panel 2 — Tracks
    def _panel_tracks(self, root):
        lf = ttk.LabelFrame(root, text="Tracks", padding=(4, 4))
        lf.grid(row=0, column=1, sticky='nsew', padx=4)
        lf.rowconfigure(0, weight=1)
        lf.columnconfigure(0, weight=1)

        frame, self.track_tree = self._scrolled_tree(
            lf,
            columns= ('ic', 'artist', 'title'),
            headings=('',   'Artist',  'Title'),
            widths=  (26,    160,       220),
            stretch= {'artist', 'title'})
        self.track_tree.column('ic', anchor='center', stretch=False)
        frame.grid(row=0, column=0, sticky='nsew')

    # Panel 3 — Modifiers
    def _panel_modifiers(self, root):
        lf = ttk.LabelFrame(root, text="Modifiers", padding=(8, 6))
        lf.grid(row=0, column=2, sticky='nsew', padx=4)

        self._opts = {}
        checks = [
            ('strip_parens',      'Strip ( ) [ ]'),
            ('strip_suffixes',    'Strip suffixes'),
            ('strip_articles',    'Strip articles (the, der …)'),
            ('strip_apostrophes', 'Strip apostrophes'),
            ('collapse_spaces',   'Collapsed-space match'),
            ('trailing_g',        "Trailing-g  (dancin'=dancing)"),
            ('drop_singles',      'Drop single chars'),
            ('truncated',         'Truncated-title match'),
        ]
        sec = ttk.LabelFrame(lf, text="Text normalization", padding=(6, 4))
        sec.pack(fill='x', pady=(0, 6))
        for key, label in checks:
            var = tk.BooleanVar(value=True)
            self._opts[key] = var
            ttk.Checkbutton(sec, text=label, variable=var).pack(anchor='w', pady=1)

        lib = ttk.LabelFrame(lf, text="Library filter", padding=(6, 4))
        lib.pack(fill='x', pady=(0, 6))
        var2 = tk.BooleanVar(value=True)
        self._opts['marked_only'] = var2
        ttk.Checkbutton(lib, text='Stars / Comment only',
                        variable=var2).pack(anchor='w', pady=1)

        svc = ttk.LabelFrame(lf, text="Replace services", padding=(6, 4))
        svc.pack(fill='x', pady=(0, 10))
        for key, label in [('replace_spotify', '🎵  Spotify'),
                            ('replace_tidal',   '🌊  Tidal')]:
            var3 = tk.BooleanVar(value=True)
            self._opts[key] = var3
            ttk.Checkbutton(svc, text=label, variable=var3).pack(anchor='w', pady=1)

        self._analyze_btn = ttk.Button(lf, text="▶   Analyze",
                                       style='Run.TButton', command=self._run)
        self._analyze_btn.pack(fill='x', pady=(0, 4))
        ttk.Button(lf, text="💾  Save playlist as …",
                   style='Save.TButton', command=self._save).pack(fill='x')

        self._progress = ttk.Progressbar(lf, mode='indeterminate', length=200)
        self._progress.pack(fill='x', pady=(6, 0))
        self._progress.pack_forget()

        # Status-Box: Container bleibt fix positioniert; Höhe wird per configure gesteuert
        self._status_frame = tk.Frame(lf, bg=BG2, height=0)
        self._status_frame.pack(fill='x', pady=(6, 0))
        self._status_frame.pack_propagate(False)
        self._status_text = tk.Text(self._status_frame, state='disabled',
                                    bg=BG2, fg=FG_DIM, relief='flat',
                                    font=('Helvetica', 11), wrap='word',
                                    borderwidth=0, highlightthickness=0,
                                    padx=4, pady=4)
        self._status_text.pack(fill='both', expand=True)

    _STATUS_LINE_H = 18   # px pro Zeile (ungefähr)

    def _set_status(self, msg: str):
        self._status_text.configure(state='normal')
        self._status_text.delete('1.0', tk.END)
        self._status_text.insert('1.0', msg)
        self._status_text.configure(state='disabled')
        lines = msg.count('\n') + 1 if msg else 0
        self._status_frame.configure(height=lines * self._STATUS_LINE_H + 8 if lines else 0)

    # Panel 4 — Result
    def _panel_result(self, root):
        lf = ttk.LabelFrame(root, text="Result", padding=(4, 4))
        lf.grid(row=0, column=3, sticky='nsew', padx=(4, 0))
        lf.rowconfigure(0, weight=1)
        lf.columnconfigure(0, weight=1)

        frame, self.result_tree = self._scrolled_tree(
            lf,
            columns= ('sel', 'ic', 'artist', 'title', 'match'),
            headings=('',    '',   'Artist',  'Title',  '→ Local file'),
            widths=  (28,    26,    140,       170,      220),
            stretch= {'artist', 'title', 'match'})
        self.result_tree.column('sel', anchor='center', stretch=False)
        self.result_tree.column('ic',  anchor='center', stretch=False)

        self.result_tree.tag_configure('exact',     foreground=ACC_GREEN)
        self.result_tree.tag_configure('exact_off', foreground=FG_DIM)
        self.result_tree.tag_configure('fuzzy',     foreground=ACC_YEL)
        self.result_tree.tag_configure('fuzzy_off', foreground=FG_DIM)
        self.result_tree.tag_configure('spotify',   foreground=FG_DIM)
        self.result_tree.tag_configure('local',     foreground=ACC_BLUE)

        self._result_toggleable: set  = set()
        self._result_enabled:    dict = {}
        self.result_tree.bind('<ButtonRelease-1>', self._on_result_click)

        frame.grid(row=0, column=0, sticky='nsew')

    # ── Daten-Logik ────────────────────────────────────────────────────────────
    def _load_db(self):
        if not VDJ_DB.exists():
            messagebox.showerror("Fehler",
                f"database.xml nicht gefunden:\n{VDJ_DB}")
            return
        try:
            self._db_root = ET.parse(VDJ_DB).getroot()
        except Exception as e:
            messagebox.showerror("Fehler", f"database.xml nicht lesbar:\n{e}")

    def _load_playlists(self):
        self.pl_list.delete(0, tk.END)
        self._playlist_paths = sorted(MYLISTS.glob("*.vdjfolder"))
        for p in self._playlist_paths:
            self.pl_list.insert(tk.END, "  " + p.stem)

    def _on_playlist_select(self, _):
        sel = self.pl_list.curselection()
        if not sel: return
        path = self._playlist_paths[sel[0]]
        self._current_path = path
        self._load_tracks(path)
        self.result_tree.delete(*self.result_tree.get_children())
        self._result_data = []
        self._set_status("")

    def _load_tracks(self, path: Path):
        self.track_tree.delete(*self.track_tree.get_children())
        try:
            root = ET.parse(path).getroot()
        except Exception as e:
            messagebox.showerror("Fehler", str(e)); return
        self._current_songs = root.findall("song")
        svc_counts = {}
        for song in self._current_songs:
            svc = get_service(song)
            if svc:
                svc_counts[svc] = svc_counts.get(svc, 0) + 1
            icon   = _SERVICE_ICON.get(svc, "🎵") if svc else "💿"
            artist = song.get("artist", "")
            title  = song.get("title",  "")
            self.track_tree.insert('', 'end', values=(icon, artist, title))

        n       = len(self._current_songs)
        svc_str = "  ".join(f"{_SERVICE_ICON.get(k,'?')} {v}"
                            for k, v in svc_counts.items())
        self.title(f"VDJ  Spotify → Lokal  —  {path.stem}  "
                   f"({n} tracks" + (f", {svc_str}" if svc_str else "") + ")")

    def _get_opts(self) -> dict:
        return {k: v.get() for k, v in self._opts.items()}

    # ── Analyse ────────────────────────────────────────────────────────────────
    def _run(self):
        if not self._current_path:
            messagebox.showwarning("Keine Playlist",
                "Bitte erst eine Playlist wählen."); return
        if self._db_root is None:
            messagebox.showerror("Fehler", "database.xml nicht geladen."); return

        self.result_tree.delete(*self.result_tree.get_children())
        self._result_data       = []
        self._result_toggleable = set()
        self._result_enabled    = {}
        self._result_candidates = {}
        self._result_choice     = {}
        self._set_status("")
        self._analyze_btn.configure(state='disabled')
        self._progress.pack(fill='x', pady=(6, 0))
        self._progress.start(12)
        self._set_status("Lade Library …")

        threading.Thread(target=self._run_worker, daemon=True).start()

    def _run_worker(self):
        try:
            self._run_inner()
        except Exception as e:
            self.after(0, lambda: messagebox.showerror("Fehler", str(e)))
        finally:
            self.after(0, self._run_done)

    def _run_done(self):
        self._progress.stop()
        self._progress.pack_forget()
        self._analyze_btn.configure(state='normal')

    def _run_inner(self):
        opts = self._get_opts()
        marked_only     = opts.pop('marked_only',     True)
        replace_spotify = opts.pop('replace_spotify', True)
        replace_tidal   = opts.pop('replace_tidal',   True)
        replace_services = {s for s, active in
                            [('spotify', replace_spotify), ('tidal', replace_tidal)]
                            if active}
        if replace_services:
            replace_services.add('streaming')

        local_tracks, n_missing = build_local_index(self._db_root, marked_only)
        self.after(0, lambda n=len(local_tracks):
                   self._set_status(f"Matche gegen {n} Tracks …"))

        rows = []
        n_exact = n_fuzzy = n_spotify = n_local = 0

        for song in self._current_songs:
            artist = song.get("artist", "")
            title  = song.get("title",  "")
            svc    = get_service(song)

            if not svc:
                rows.append(('local', song, None,
                             ('', '💿', artist, title, '(already local)'), 'local', None))
                n_local += 1
                continue

            if svc not in replace_services:
                icon = _SERVICE_ICON.get(svc, '🎵')
                rows.append(('none', song, None,
                             ('', icon, artist, title, f'({svc}, skipped)'), 'spotify', None))
                n_spotify += 1
                continue

            match_type, candidates = find_matches(artist, title, local_tracks, opts)

            if match_type == 'exact':
                local_song = candidates[0]
                rows.append(('exact', song, local_song,
                             ('☑', '✅', artist, title,
                              self._match_label(local_song, candidates)),
                             'exact', candidates))
                n_exact += 1
            elif match_type == 'fuzzy':
                local_song = candidates[0]
                rows.append(('fuzzy', song, local_song,
                             ('☑', '🟡', artist, title,
                              self._match_label(local_song, candidates)),
                             'fuzzy', candidates))
                n_fuzzy += 1
            else:
                icon = _SERVICE_ICON.get(svc, '🎵')
                rows.append(('none', song, None,
                             ('', icon, artist, title, '—'), 'spotify', None))
                n_spotify += 1

        total  = n_exact + n_fuzzy + n_spotify + n_local
        status = (
            f"✅  Ersetzt     {n_exact}\n"
            f"🟡  Unsicher   {n_fuzzy}\n"
            f"🎵  Spotify    {n_spotify}\n"
            f"💿  Lokal      {n_local}\n"
            f"─────────────────\n"
            f"Gesamt         {total}\n"
            f"Library        {len(local_tracks)}"
            + (f"\nFehlend        {n_missing}" if n_missing else "")
        )
        self.after(0, lambda: self._populate_results(rows, status))

    def _populate_results(self, rows, status):
        for (match_type, orig, local, values, tag, candidates) in rows:
            iid = self.result_tree.insert('', 'end', values=values, tags=(tag,))
            self._result_data.append((match_type, orig, local, iid))
            if match_type in ('exact', 'fuzzy'):
                self._result_toggleable.add(iid)
                self._result_enabled[iid] = True
            if candidates and len(candidates) > 1:
                self._result_candidates[iid] = candidates
        self._set_status(status)
        if self._result_candidates:
            self.after(50, self._resolve_duplicates)

    @staticmethod
    def _match_label(local: ET.Element, candidates: list) -> str:
        fname = Path(local.get('FilePath', '')).name
        return f"{fname}   ⧉ {len(candidates)}" if len(candidates) > 1 else fname

    def _resolve_duplicates(self):
        """Nach der Analyse: für jeden Track mit mehreren lokalen Kandidaten fragen."""
        iids = list(self._result_candidates)
        for n, iid in enumerate(iids, 1):
            if not self._pick_candidate(iid, progress=f"{n} / {len(iids)}"):
                break

    def _pick_candidate(self, iid, progress: str = "") -> bool:
        """Öffnet den Auswahldialog. Rückgabe False = 'Alle überspringen'."""
        candidates = self._result_candidates[iid]
        vals = list(self.result_tree.item(iid, 'values'))
        current = self._result_choice.get(iid) or next(
            local for (_, _, local, i) in self._result_data if i == iid)
        chosen, skip_all = _ask_candidate(self, vals[2], vals[3], candidates,
                                          current, progress)
        if chosen is not None:
            self._result_choice[iid] = chosen
            vals[4] = self._match_label(chosen, candidates)
            self.result_tree.item(iid, values=vals)
            if not self._result_enabled.get(iid, True):
                self._toggle_result(iid)
        return not skip_all

    # ── Speichern ──────────────────────────────────────────────────────────────
    def _save(self):
        try:
            self._save_inner()
        except Exception as exc:
            messagebox.showerror("Fehler beim Speichern",
                f"{exc}\n\n{traceback.format_exc()}", parent=self)

    def _save_inner(self):
        if not self._result_data:
            messagebox.showwarning("Kein Ergebnis",
                "Bitte erst Analyze ausführen."); return

        default = (self._current_path.stem if self._current_path else "playlist") + "-FIX"
        name = _ask_string(self, "Speichern als", "Name der neuen Playlist:", default)
        if not name: return
        name = name.strip()
        if not name: return

        MYLISTS.mkdir(parents=True, exist_ok=True)
        target = MYLISTS / f"{name}.vdjfolder"
        if target.exists():
            if not messagebox.askyesno("Überschreiben?",
                    f'"{name}.vdjfolder" existiert bereits.\nÜberschreiben?',
                    parent=self):
                return

        new_root = ET.Element("VirtualFolder", noDuplicates="yes")
        idx = 0

        for (match_type, orig, local, iid) in self._result_data:
            artist = orig.get("artist", "")
            title  = orig.get("title",  "")
            swap   = self._result_enabled.get(iid, False)
            local  = self._result_choice.get(iid, local)

            if match_type in ('local', 'none') or not swap:
                el = ET.SubElement(new_root, "song")
                el.attrib.update(orig.attrib)
                el.set("idx", str(idx)); idx += 1

            elif match_type == 'exact':
                el = self._make_local_el(new_root, local, artist, title)
                el.set("idx", str(idx)); idx += 1

            elif match_type == 'fuzzy':
                el_loc = self._make_local_el(new_root, local, artist, title, color=YELLOW)
                el_loc.set("idx", str(idx)); idx += 1
                el_sp = ET.SubElement(new_root, "song")
                el_sp.attrib.update(orig.attrib)
                el_sp.set("idx", str(idx)); idx += 1

        ET.indent(new_root, space="\t")
        ET.ElementTree(new_root).write(target, encoding="UTF-8", xml_declaration=True)

        self._load_playlists()
        stems = [p.stem for p in self._playlist_paths]
        if name in stems:
            i = stems.index(name)
            self.pl_list.selection_clear(0, tk.END)
            self.pl_list.selection_set(i)
            self.pl_list.see(i)
            self._current_path = self._playlist_paths[i]

        messagebox.showinfo("Gespeichert",
            f'✅  "{name}.vdjfolder" gespeichert!\n'
            f'VDJ neu starten, damit die Liste erscheint.', parent=self)

    # ── Result-Interaktion ─────────────────────────────────────────────────────
    def _on_result_click(self, event):
        iid = self.result_tree.identify_row(event.y)
        if not iid or iid not in self._result_toggleable:
            return
        # Klick auf "→ Local file" bei mehreren Kandidaten öffnet die Auswahl
        if (iid in self._result_candidates
                and self.result_tree.identify_column(event.x) == '#5'):
            self._pick_candidate(iid)
            return
        self._toggle_result(iid)

    def _toggle_result(self, iid):
        enabled = not self._result_enabled.get(iid, True)
        self._result_enabled[iid] = enabled

        vals = list(self.result_tree.item(iid, 'values'))
        vals[0] = '☑' if enabled else '☐'
        current_tags = list(self.result_tree.item(iid, 'tags'))
        base = None
        for t in current_tags:
            if t in ('exact', 'exact_off', 'fuzzy', 'fuzzy_off'):
                base = t.rstrip('_off')
                break
        self.result_tree.item(iid, values=vals,
                              tags=(base if enabled else f'{base}_off',))

    @staticmethod
    def _make_local_el(parent: ET.Element, song: ET.Element,
                       fallback_artist: str, fallback_title: str,
                       color=None) -> ET.Element:
        tags = song.find("Tags")
        el = ET.SubElement(parent, "song")
        el.set("path",   song.get("FilePath", ""))
        el.set("artist", tags.get("Author", fallback_artist) if tags is not None else fallback_artist)
        el.set("title",  tags.get("Title",  fallback_title)  if tags is not None else fallback_title)
        if tags is not None:
            for attr in ("Bpm", "Key"):
                v = tags.get(attr, "")
                if v: el.set(attr.lower(), v)
        if color is not None:
            el.set("color", color)
        return el


# ── Entry point ────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    app = App()
    app.mainloop()
