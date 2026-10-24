# VirtualDJ Playlist Destreamer

Replaces Spotify / Tidal tracks in a VirtualDJ playlist with locally owned copies — without touching your original files.

![UI Screenshot](screenshot.png)

---

## What it does

VirtualDJ stores playlists (`.vdjfolder`) as XML files. When you've been mixing from Spotify or Tidal and later buy or rip those albums, this tool finds the matches in your local library and produces a **new** playlist with the streaming entries swapped out for the local files.

- Scans your `MyLists/*.vdjfolder` playlists
- Matches Spotify / Tidal tracks against your `database.xml` using fuzzy title + artist normalization
- Lets you **toggle** each match on/off before saving
- Writes a new `.vdjfolder` — your originals are never modified
- Handles umlauts, parenthetical suffixes, articles, apostrophes, trailing-g variations, and single-character noise

---

## Requirements

- **macOS** (paths default to `~/Library/Application Support/VirtualDJ`)
- **Python 3.10+**
- **VirtualDJ** with a populated `database.xml`
- Optional: [`mutagen`](https://pypi.org/project/mutagen/) for deeper local-track detection via embedded tags

---

## Installation & running (venv)

```bash
# 1. Clone
git clone https://github.com/andymann/virtualdj-playlist-destreamer.git
cd virtualdj-playlist-destreamer

# 2. Create and activate a virtual environment
python3 -m venv .venv
source .venv/bin/activate

# 3. Install optional dependency (recommended)
pip install mutagen

# 4. Run
python vdj_spotify_ui.py
```

To launch again later:

```bash
cd virtualdj-playlist-destreamer
source .venv/bin/activate
python vdj_spotify_ui.py
```

> **Note:** Close VirtualDJ before saving a playlist, otherwise VDJ will overwrite your new file on exit.

---

## How to use

1. **Pick a playlist** from the left panel — tracks load automatically.
2. **Adjust modifiers** in the middle panel (defaults work well for most libraries).
3. Click **▶ Analyze** — the Result panel shows every track with its match status:
   - ✅ Green = confident local match
   - 🟡 Yellow = fuzzy match (artist word overlap only) — review these
   - 🎵 = no local copy found / service not selected for replacement
   - 💿 = already a local file, kept as-is
4. **Click any matched row** to toggle it off (☐) if you don't want that substitution.
5. Click **💾 Save playlist as …**, enter a name, and the new `.vdjfolder` lands in `MyLists/`.
6. Restart VirtualDJ — the new playlist appears in your browser.

---

## Modifier options

| Option | Effect |
|---|---|
| Strip `( ) [ ]` | Removes parenthetical suffixes before matching |
| Strip suffixes | Drops "Remastered", "Radio Edit", "Live", etc. |
| Strip articles | Ignores "The", "Der", "Les", … |
| Strip apostrophes | `it's` → `its` |
| Collapsed-space match | `hiphop` matches `hip hop` |
| Trailing-g | `dancin'` matches `dancing` |
| Drop single chars | Removes stray single-character tokens |
| Stars / Comment only | Only considers DB tracks that have a star rating or comment (faster, fewer false positives) |
| 🎵 Spotify / 🌊 Tidal | Choose which services to replace |

---

## Local track detection

A track counts as "local" if its `database.xml` entry has:
- A real file path (not a `netsearch://` or `.vdjcache` path)
- No `netsearchId` attribute (streaming tracks VDJ cached with metadata)
- With **Stars / Comment only** enabled: at least one star or a comment tag

---

## License

MIT
