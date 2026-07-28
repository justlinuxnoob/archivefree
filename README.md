<div align="center">

<img src="data/icons/hicolor/scalable/apps/io.github.justlinuxnoob.ArchiveFree.svg" width="112" alt="ArchiveFree">

# ArchiveFree

**Look inside an archive before anything gets unpacked.**

A free, ad-free archive manager for Linux.

</div>

---

## What it does

Double-click a `.zip` on most Linux desktops and your files are scattered across
the current folder before you've had a chance to look at them.

ArchiveFree opens a window instead. You see what's inside — folders, file sizes,
dates — and *then* you decide: extract everything, extract the two files you
actually wanted, or extract somewhere else entirely. Nothing touches your disk
until you say so.

<div align="center">

*Browse first. Extract second.*

</div>

### The rest of it

- **Opens what you'll actually run into** — zip, 7z, rar, tar and every
  compressed flavour of it (gz, bz2, xz, zst), plain gz/bz2/xz/zst, iso, cab,
  deb, rpm, lha, arj, cpio, wim, dmg and more.
- **Preview without extracting** — read a text file, a README or a config, or
  look at an image, straight out of the archive.
- **Never silently overwrites** — if a file already exists it asks, and shows
  you both versions so the choice is an informed one.
- **Passwords and split archives** — prompts when a password is needed, and
  finds every part of a multi-part archive automatically.
- **Makes new archives too** — pick a format, pick how hard to compress, add a
  password or split it into parts if you want.
- **Stays responsive** — a 4 GB archive won't freeze the window, and you can
  cancel anything mid-way.
- **Explains itself when things go wrong** — "this archive is damaged, it may
  not have finished downloading" instead of a stack trace.

No adverts. No telemetry. No accounts. No paid tier. Not now, not later.

---

## Install

### Debian, Ubuntu, MX Linux, Mint, Pop!\_OS

Download `archivefree_<version>_all.deb` from the
[latest release](https://github.com/justlinuxnoob/archivefree/releases/latest),
then double-click it — or, in a terminal:

```bash
sudo apt install ./archivefree_*_all.deb
```

`apt` will pull in anything else it needs. To also handle 7z, rar, iso and
encrypted zip files:

```bash
sudo apt install 7zip
```

### Flatpak (any distribution)

```bash
flatpak install flathub io.github.justlinuxnoob.ArchiveFree
```

> **Note:** the Flathub submission is still pending. Until it's accepted, grab
> `archivefree.flatpak` from the
> [latest release](https://github.com/justlinuxnoob/archivefree/releases/latest)
> and install it directly:
>
> ```bash
> flatpak install --user archivefree.flatpak
> ```

The Flatpak bundles its own 7-Zip, so every supported format works out of the
box with nothing else to install.

### Make it the default

The first time you run ArchiveFree it offers to become the app that opens
archive files. That's one click, it doesn't ask for your password, and you can
undo it any time from **Preferences → Default Archive Application**.

---

## Formats

| Format | Open | Create | Needs |
|---|:---:|:---:|---|
| `.zip` | ✅ | ✅ | built in |
| `.tar`, `.tar.gz`, `.tar.bz2`, `.tar.xz` | ✅ | ✅ | built in |
| `.gz`, `.bz2`, `.xz` (single files) | ✅ | — | built in |
| `.tar.zst`, `.zst` | ✅ | ✅ | `zstd` |
| `.7z` | ✅ | ✅ | `7zip` |
| `.rar` | ✅ | — | `7zip` or `unrar` |
| `.iso`, `.cab`, `.dmg`, `.wim`, `.msi`, `.xar` | ✅ | — | `7zip` |
| `.deb`, `.rpm`, `.cpio`, `.lha`, `.arj`, `.ar` | ✅ | — | `7zip` |
| Password-protected zip (AES) | ✅ | ✅ | `7zip` |
| Split / multi-part archives | ✅ | ✅ | `7zip` |

**Preferences → Formats** shows which helper programs you have and what each one
would add. If a format needs something you don't have, ArchiveFree tells you the
exact command to install it rather than just failing.

---

## Keyboard shortcuts

| | |
|---|---|
| <kbd>Ctrl</kbd>+<kbd>O</kbd> | Open an archive |
| <kbd>Ctrl</kbd>+<kbd>N</kbd> | New archive |
| <kbd>Ctrl</kbd>+<kbd>E</kbd> | Extract (selection, or everything) |
| <kbd>Ctrl</kbd>+<kbd>F</kbd> | Search inside the archive |
| <kbd>Space</kbd> | Preview the selected file |
| <kbd>Alt</kbd>+<kbd>↑</kbd> | Go to the parent folder |
| <kbd>Esc</kbd> | Clear search, or cancel the current operation |

---

## Command line

ArchiveFree is a graphical app, but it takes a few flags so file managers (and
you) can drive it:

```bash
archivefree photos.zip                 # open it in a window
archivefree --extract-here photos.zip  # extract beside the archive
archivefree --extract-to ~/Pictures photos.zip
archivefree --new-archive ~/Documents  # open the create dialog, pre-filled
```

---

## Building from source

<details>
<summary><b>For developers</b></summary>

### What you need

ArchiveFree is pure Python — there is nothing to compile.

```bash
# Debian / Ubuntu / MX
sudo apt install python3 python3-gi gir1.2-gtk-4.0 gir1.2-adw-1 7zip

# Fedora
sudo dnf install python3 python3-gobject gtk4 libadwaita p7zip

# Arch
sudo pacman -S python python-gobject gtk4 libadwaita 7zip
```

Requires Python 3.11+, GTK 4.10+ and libadwaita 1.4+.

### Run it without installing

```bash
git clone https://github.com/justlinuxnoob/archivefree.git
cd archivefree
./run-dev.sh                      # or: ./run-dev.sh some-archive.zip
```

### Tests

```bash
python3 -m pytest tests/ -v
```

The suite round-trips real archives in every supported format and cross-checks
the results against the system's own `unzip`, `tar` and `7z`, so it catches
"works against my own code" bugs. It also covers passwords, split volumes,
truncated archives, name collisions and path-traversal attacks.

### Build the packages

```bash
packaging/deb/build.sh                      # -> dist/archivefree_<version>_all.deb
flatpak-builder --repo=repo build-dir \
    packaging/flatpak/io.github.justlinuxnoob.ArchiveFree.yml
```

### How it fits together

```
archivefree/
├── core/            # no GTK imports anywhere in here — importable and testable headless
│   ├── detect.py    # format sniffing by magic bytes, extension as tie-breaker
│   ├── registry.py  # picks a backend: stdlib first, CLI tools as fallback
│   ├── backends/    # zip, tar, single-stream, 7-Zip, unrar
│   ├── jobs.py      # worker threads, progress, cancellation
│   ├── tree.py      # flat entry list -> browsable folder tree
│   └── create.py    # making archives
├── ui/              # GTK4 + libadwaita; talks to core only through jobs
└── integration/     # MIME defaults and file-manager menus
```

The rule the codebase follows: **backends never touch GTK.** They report
progress through a `Progress` object, and `jobs.py` marshals that onto the main
loop. That's what keeps the interface responsive and the core unit-testable
without a display.

</details>

---

## Contributing

Bug reports and patches are welcome — see [CONTRIBUTING.md](CONTRIBUTING.md).
If ArchiveFree failed to open an archive you care about, that's the most useful
report you can file.

## Licence

[GPL-3.0-or-later](LICENSE). Free software, and it stays that way.
