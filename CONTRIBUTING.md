# Contributing to ArchiveFree

Thanks for taking the time. This is a small, practical project and every kind of
help is welcome — including the kind that isn't code.

## The most useful thing you can do

**Tell us about an archive that didn't open.** Format coverage is the whole
point of an archive manager, and real files from the wild find bugs that
synthetic test archives never will.

If you can, include:

- what the file is (`file yourarchive.xyz` output is perfect)
- what ArchiveFree said, including anything under **Technical details**
- whether another tool opens it (`7z l yourarchive.xyz`, `unzip -t …`)

You don't need to attach the archive itself — often the `file` output and the
error are enough.

## Reporting a bug

Use the [bug report template](https://github.com/justlinuxnoob/archivefree/issues/new?template=bug_report.yml).
The one thing worth doing carefully is the reproduction steps: "opened a 2 GB
zip made by Windows Explorer, clicked Extract, it stopped at 40%" is far more
actionable than "extraction is broken".

The **About → Troubleshooting** section of the app has a *Debug Information*
block — copying that in saves a round-trip.

## Setting up

There is nothing to compile.

```bash
# Debian / Ubuntu / MX
sudo apt install python3 python3-gi gir1.2-gtk-4.0 gir1.2-adw-1 7zip

git clone https://github.com/justlinuxnoob/archivefree.git
cd archivefree
./run-dev.sh                 # runs straight from the source tree
python3 -m pytest tests/ -v
```

> **If GTK 4 fails to import** on a distribution that ships the libraries but
> not their typelibs, drop the `.typelib` files into `.devtypelib/` and
> `run-dev.sh` will pick them up.

## How the code is arranged

```
archivefree/
├── core/          # archive logic — no GTK imports anywhere in this directory
├── ui/            # GTK 4 + libadwaita
└── integration/   # MIME defaults, file-manager menus
```

Two rules keep the codebase sane. Please hold to them:

1. **`core/` never imports GTK.** That's what lets the whole engine be tested
   headlessly, and it's why the test suite runs in CI without a display. If you
   need to tell the UI something from a backend, report it through the
   `Progress` object.

2. **Nothing blocks the main loop.** Anything that touches a file goes through
   `core/jobs.py`, which runs it on a worker thread and marshals callbacks back
   with `GLib.idle_add`. A frozen window is a bug, even if the operation
   finishes.

## Adding support for a format

1. If `7z` can already read it, you may only need to add it to
   `core/detect.py` (magic bytes + extension) and to `SevenZipBackend.formats`.
2. If it needs its own handling, add a backend under `core/backends/`
   subclassing `Backend`, and register it in `core/registry.py`. Give it a
   `priority` above `SevenZipBackend`'s 10 if it should win.
3. Add the MIME type to `data/*.desktop` and to `HANDLED_TYPES` in
   `integration/defaults.py`.
4. **Add a round-trip test.** See below.

## Tests

```bash
python3 -m pytest tests/ -v
```

The suite deliberately cross-checks against the system's own `unzip`, `tar` and
`7z` rather than only against ArchiveFree. Verifying our code against our code
proves only that it's self-consistent — a test that creates an archive and then
reads it back with our own reader will happily pass while producing files no
other tool can open.

So a new format test should do both directions:

- create an archive with ArchiveFree, then verify a system tool can read it
- create one with a system tool, then verify ArchiveFree extracts it byte-for-byte

`tests/test_edge_cases.py` covers the unhappy paths — passwords, split volumes,
truncated files, name collisions, and path-traversal ("zip slip") attacks.
**Please don't weaken the security tests to make something pass.** If an archive
wants to write outside the destination, refusing it is correct behaviour.

## Style

- Run `ruff check archivefree tests` before opening a PR.
- Match the surrounding code. Type hints on new functions, docstrings on
  anything non-obvious.
- Comments should explain *why*, not restate the code.

### User-facing text

This matters more than usual here, because a big part of the point of
ArchiveFree is that it explains itself to people who aren't developers.

- Write plain sentences. "This archive is damaged." — not "CRC mismatch in
  central directory".
- Say what the person can do about it. Errors carry a `hint` field for exactly
  this.
- Put the technical cause in `detail`; it goes in the collapsed section.
- Never show a raw traceback or a tool's stderr as the primary message.

## Pull requests

Small and focused is easier to review than large and sweeping. Say what you
changed and why; if it changes what the user sees, a screenshot helps a lot.

By contributing you agree your work is licensed under
[GPL-3.0-or-later](LICENSE).

## What's out of scope

- Adverts, telemetry, analytics, crash reporting that phones home, "optional"
  accounts, or a paid tier. This is a permanent no, not a not-yet.
- Bundling non-free software in the Flatpak (which is why RAR support there goes
  through 7-Zip rather than `unrar`).
