# EG Player

A fast, lightweight Windows video player with frame-accurate scrubbing, playback speed in any
percentage, pixel-level detail viewing and a small DaVinci Resolve-style editor.

- **Plays almost any video:** MP4, MKV, MOV, AVI, WebM, WMV, FLV, TS/M2TS, MPG, VOB, MXF, 3GP, OGV
  and many more. Codecs include H.264, HEVC/H.265 (incl. 10-bit/HDR), AV1, VP9, ProRes, DNxHD, MPEG-2/4,
  FFV1 and others. Playback uses libmpv/FFmpeg with GPU decoding (NVDEC/D3D11/DXVA2) and falls back
  to the CPU when needed.
- **Picture quality:** frames are decoded bit-exact. There is no debanding, denoising or sharpening.
  Upscaling uses EWA Lanczos with gamma-correct downscaling. At **1:1** each video pixel is one screen pixel.
- **Frame-by-frame scrubbing:**
  - `←`/`→` step one frame (hold to repeat). Backward stepping is exact too.
  - Right-drag on the video to jog frames.
  - Scroll over the seek bar to step frames.
  - Dragging the seek bar always lands on exact frames.
  - `J`/`K`/`L` shuttle.
- **Any speed:** type any value from 1 % to 10 000 % (e.g. 37.5 %). Audio pitch is kept natural.
  `[` `]` change speed by ±10 % and `Ctrl+[` `Ctrl+]` by ±1 %.
- **Fine detail viewing:**
  - Mouse-wheel zoom toward the cursor, up to 6400 %, and drag to pan.
  - **Pixel Peep** (`P`): nearest-neighbour zoom, a pixel grid, and the exact X/Y and RGB of the
    decoded source pixel under the cursor (pause to read RGB).
  - Lossless PNG frame grabs (`Ctrl+Shift+S`).
  - Media-info overlay (`Ctrl+I`).
- **Mini editor** (`Ctrl+E`), laid out like Resolve's Edit page:
  - Media pool with thumbnails.
  - Source viewer with In/Out marks (`I`/`O`). `F12` appends the marked range to the timeline and
    `F9` inserts it at the playhead.
  - Magnetic V1+A1 timeline with waveforms. Drag clips to reorder and drag edges to ripple-trim.
    `B` splits at the playhead (razor) and `Del` deletes with ripple. Undo/redo is available.
  - Inspector for per-clip **speed %**, volume, fade in/out, and colour (brightness, contrast,
    saturation, gamma, hue). Colour changes preview live. The export reproduces them to within
    ±2–3/255 of the preview.
  - `Tab` switches the viewer between Source and Timeline, so the edit plays back seamlessly.
  - Projects save as `.egproj`.
- **Export** (`Ctrl+M`):

  | Preset | What it is |
  |---|---|
  | Lossless — FFV1 + FLAC (.mkv) | Bit-exact archive (verified: decoded frames identical to source) |
  | Lossless — H.264 qp 0 + FLAC (.mkv) | Bit-exact, much smaller |
  | Lossless — stream copy (.mkv) | No re-encode at all. Cuts snap to keyframes and clip effects are ignored |
  | ProRes 422 HQ / 4444 XQ (.mov) | Edit-ready intermediates for Resolve / Premiere / FCP |
  | H.264 CRF 14, H.265 10-bit CRF 16, AV1, H.264 CRF 20 | High quality and sharing |

  Resolution and frame-rate conform are available, and you can view the exact FFmpeg command.
  `Ctrl+Shift+E` exports just the viewer's In→Out range.

## Installing

Download `EGPlayer-Setup-<version>.exe` from the
[latest release](https://github.com/Emmanuel-grams/eg-player/releases/latest) and run it. The
installer isn't code-signed, so Windows SmartScreen may show "Windows protected your PC" the first
time: click **More info → Run anyway**. The installer:

- installs for the current user into `%LOCALAPPDATA%\Programs\EG Player` with no admin prompt
  (choose "Install for all users" in the first dialog if you want Program Files instead);
- adds Start-menu and, optionally, desktop shortcuts, plus an *Apps & features* entry with a proper
  uninstaller;
- adds **EG Player to "Open with"** for 92 video and audio file types and registers it under
  *Settings → Apps → Default apps*. It never takes over your existing defaults. To make it the
  default player, right-click a video → *Open with* → *Choose another app* → EG Player → *Always*.

Uninstall from *Settings → Apps*. That removes the files and every "Open with" entry and keeps
your preferences.

## Updating

The status bar shows **v1.x.y · Check for updates** at the bottom right. EG Player also checks quietly
each time it starts. When a newer version exists, the button turns orange and reads
**↑ Update to x.y.z**. Click it, then **Download and install**. The installer is SHA-256 verified,
then EG Player closes, updates and starts again by itself. *Help → Check for updates…* opens the same
dialog.

Updates come from this repository's
[GitHub Releases](https://github.com/Emmanuel-grams/eg-player/releases), so every PC with EG Player
installed gets them. The **update source** can be changed in that dialog:

| Source | Example |
|---|---|
| A GitHub repository | `Emmanuel-grams/eg-player` (the default). Uses the latest Release's `EGPlayer-Setup-<ver>.exe` |
| A folder (local or network share) | `D:\Claude Code\EG-Videoplayer\releases`, or any folder `release.ps1` wrote |
| A URL to a manifest | `https://example.com/egplayer/latest.json` |

EG Player isn't code-signed, so security software treats each new version as an unknown program:

- **Antivirus may hold the installer back.** Avast's CyberCapture, for example, blocks new unsigned
  programs downloaded from the internet until it has analysed them. The update dialog then says the
  installer was blocked. Allow the file in your antivirus, or wait for its check to finish, and click
  **Download and install** again. Or click **Show installer** and run it yourself.
- **The first start after an update can take up to about a minute** while Microsoft Defender checks
  the new `EGPlayer.exe`. Later starts take about 1 s.

Signing the installer and the exe with a code-signing certificate removes both.

## Making a new version

```powershell
powershell -ExecutionPolicy Bypass -File release.ps1 -Version 1.2.0 -Notes "What changed"
```

This one command:

1. stamps the version into `egplayer\version.py` and builds `dist\EGPlayer\EGPlayer.exe`;
2. compiles `releases\EGPlayer-Setup-1.2.0.exe` with Inno Setup and writes `releases\latest.json`
   (version, SHA-256, notes), keeping the newest two installers;
3. commits and pushes the code, tags it `v1.2.0`, and publishes a GitHub Release with the installer
   attached. The notes become the release notes shown in the update dialog.

Every installed copy offers the update the next time it starts. Publishing needs the GitHub CLI
signed in (`gh auth login --web`) with push access to the repository. Add `-LocalOnly` to build into
`releases\` without publishing anything.

## Running from source

```powershell
git clone https://github.com/Emmanuel-grams/eg-player.git
cd eg-player
powershell -ExecutionPolicy Bypass -File setup.ps1 -InstallerTools  # venv, libmpv, FFmpeg, Inno Setup, gh, git
.venv\Scripts\pythonw.exe run.pyw [video file]
powershell -ExecutionPolicy Bypass -File build.ps1                  # just dist\EGPlayer\EGPlayer.exe
```

A portable or source copy, with no installer, can add itself to *Open with* through
**Tools → Add "Open with EG Player" to Explorer**. The menu hides that item in installed copies,
because the installer already handles it.

## Keyboard & mouse

Press `F1` in the app for the full list.

| Keys | Action |
|---|---|
| `Space` / `K` | Play / pause |
| `←` `→` | Previous / next frame |
| `Shift+←/→`, `Ctrl+←/→` | ±1 s, ±5 s |
| `J` / `L` | Shuttle reverse / forward (press again: 2×, 4×, 8×) |
| `[` `]`, `Ctrl+[` `Ctrl+]`, `\` | Speed −/+10 %, −/+1 %, reset to 100 % |
| Wheel / drag / middle-click | Zoom at cursor / pan / fit |
| `0` `1` `2` `4` `8` | Fit, 100 %, 200 %, 400 %, 800 % |
| `P` | Pixel Peep |
| `I` `O`, `Ctrl+L` | Mark In/Out, loop In→Out |
| `PgUp` / `PgDn` | Previous / next video in the folder |
| `F` / double-click | Fullscreen |
| `Ctrl+E`, `Tab` | Editor, Source/Timeline viewer |
| `F12` / `F9` | Append / insert In→Out |
| `B`, `Del`, `Ctrl+D` | Split, ripple delete, duplicate |
| `Ctrl+Z` / `Ctrl+Y` | Undo / redo |
| `Ctrl+M` | Export timeline |

## How it's built

- **Python 3.12 + PySide6 (Qt 6)** for the UI.
- **libmpv** renders into a `QOpenGLWidget` through mpv's render API. Its options are set for exact
  seeking (`hr-seek`), a large back-buffer for fast backward stepping, and no image post-processing.
- Timeline preview is an mpv **EDL** of the clips. Per-clip speed, volume and colour are applied live
  as the playhead crosses clip boundaries.
- **FFmpeg** (GPL build: x264, x265, SVT-AV1, ProRes, FFV1) handles probing, thumbnails, waveforms
  and rendering. Each clip is its own seeked input, so long sources render quickly.

```
egplayer/
  app.py           bootstrap, icon
  mpvwidget.py     libmpv OpenGL viewer, zoom/pan, pixel inspector, HUD
  transport.py     seek bar + transport controls
  timeline.py      timeline view (trim / move / razor / waveforms)
  panels.py        media pool + inspector
  project.py       clips, magnetic timeline, undo, EDL, projects
  exporter.py      FFmpeg graph builder + render job
  export_dialog.py deliver dialog
  media.py         ffprobe, thumbnails, waveform peaks
  updater.py       update sources (folder / GitHub / URL), download + SHA-256, silent install
  update_dialog.py "Check for updates" dialog
  version.py       app version + default update source (written by release.ps1)
  mainwindow.py    glue, menus, shortcuts, status-bar update button
installer/
  EGPlayer.iss     Inno Setup script (per-user install, shortcuts, uninstaller)
  assoc.iss        generated "Open with" / Default-apps registry entries
release.ps1        version → exe → installer → latest.json → GitHub Release
```

## Limitations

- The editor has one video track with linked audio. It has no multitrack compositing, titles or transitions
  beyond fades.
- Fades are rendered on export only; the viewer doesn't preview them.
- Reverse playback (`J`) needs the decoder to buffer whole GOPs. Some very long-GOP 4K files can't
  play backwards, and EG Player then tells you to step with `←` instead.
- Stream-copy export cuts on keyframes. Use a lossless re-encode preset for frame-exact lossless cuts.

## Licences

libmpv and FFmpeg are GPL/LGPL; the builds used here are GPL. Qt for Python is LGPL.
