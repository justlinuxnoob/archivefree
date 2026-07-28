# Submitting ArchiveFree to Flathub

Everything that can be automated is done. The manifest in this directory is
Flathub-ready, builds reproducibly in CI, and produces a bundle that installs
and runs. What remains genuinely needs a human, because Flathub submission is a
pull request reviewed by people.

## Current state

| Check | Status |
|---|---|
| Manifest builds from a clean checkout | ✅ verified in CI |
| Bundle installs and runs | ✅ verified on Debian 13 / XFCE |
| `flatpak-builder-lint manifest` | ⚠️ one error needing a reviewer decision, see below |
| Screenshots in the metainfo | ✅ four, referenced at a fixed tag |
| AppStream metadata validates | ✅ `appstreamcli validate` passes in CI |
| Desktop entry validates | ✅ `desktop-file-validate` passes in CI |
| App ID matches the repo | ✅ `io.github.justlinuxnoob.ArchiveFree` |
| Bundled dependencies are free software | ✅ 7-Zip (LGPL), built from source |

## Two linter errors that resolve themselves on Flathub

Running `flatpak-builder-lint repo repo` on a **local** build reports:

```
"appstream-screenshots-not-mirrored-in-ostree"
"appstream-external-screenshot-url"
```

Neither is a defect and neither can be fixed here. Flathub's build service
downloads the screenshot URLs from the metainfo and re-hosts them under
`https://dl.flathub.org/media`, rewriting the metainfo as it goes. That step
only happens inside Flathub's own pipeline, so a local build will always show
these. They disappear once the app is built on Flathub.

What *was* a real blocker — `metainfo-missing-screenshots` — is fixed: the
metainfo now carries four screenshots, referenced at the `v0.1.1` tag so the
URLs stay valid.

## The one linter error that needs a human decision

```
"errors": ["finish-args-host-filesystem-access"]
```

The manifest requests `--filesystem=host`. Flathub flags any broad filesystem
permission and requires the submitter to justify it in the pull request.

**This is not something to "fix" by narrowing the permission.** We tested
`--filesystem=home` plus removable media, and the linter flags that too
(`finish-args-home-filesystem-access`) — Flathub reviews *any* broad filesystem
access. Since an exception is needed either way, the honest choice is to request
the one the app actually needs.

The justification to paste into the submission PR:

> ArchiveFree is an archive manager. Its core function is to open an archive
> the user double-clicked — which can be anywhere on the filesystem — and to
> extract its contents to a destination the user picks, which can also be
> anywhere. Both the source and the destination are chosen by the user at the
> moment of use, so there is no fixed set of paths to declare in advance.
>
> The portal-based alternatives do not fit: `OpenFile` gives a single read-only
> handle, but extraction has to create an arbitrary tree of files and
> directories under a user-chosen folder, and the archive being read is chosen
> by the file manager (via the MIME association), not by a portal dialog inside
> the app.
>
> This is the same permission granted to the other archive managers on Flathub,
> including `org.gnome.FileRoller`.

If reviewers prefer the narrower permission, changing `--filesystem=host` to
`--filesystem=home`, `--filesystem=/media` and `--filesystem=/run/media` is a
three-line edit. The one functional cost is noted below.

### What the narrower permission would cost

`integration/defaults.py` reads the host's `mimeinfo.cache` from
`/run/host/usr/share/applications` to discover which application handled archive
files *before* ArchiveFree took over, so that "undo" in Preferences hands them
back to the right app. `/run/host` is only mounted with `--filesystem=host`.

Without it, undo still removes our associations cleanly, but can no longer name
the previous handler for apps installed under `/usr` — so on a system where,
say, Engrampa was the incumbent, undoing would leave no explicit default rather
than restoring Engrampa. The code already degrades gracefully; it just becomes
less faithful.

## What a human needs to do

1. **Fork** https://github.com/flathub/flathub and create a branch named
   `io.github.justlinuxnoob.ArchiveFree` (branch name must equal the app ID).

2. **Add the manifest** at the repository root as
   `io.github.justlinuxnoob.ArchiveFree.yml`. Take it from this directory, with
   one change: replace the `type: dir` source in the `archivefree` module with a
   pinned git source, so Flathub builds from a tag rather than a local path:

   ```yaml
       sources:
         - type: git
           url: https://github.com/justlinuxnoob/archivefree.git
           tag: v0.1.1
           commit: 7f242e70a8faefb36ef89e9772f116c686844f7f
   ```

   Get the SHA with:

   ```bash
   git rev-parse v0.1.1^{commit}
   ```

   Flathub requires both `tag` and `commit` — the commit is what actually pins
   the build.

3. **Also copy** `packaging/flatpak/archivefree-launcher` handling: the build
   commands reference it by path inside the checkout, so no change is needed
   once the git source is in place.

4. **Open the pull request** against `flathub/flathub`, targeting the `new-pr`
   branch. In the description:
   - state that you are the upstream author
   - paste the filesystem justification above
   - confirm the app has no adverts, telemetry or network access

5. **Wait for review.** A bot builds the manifest and comments. Reviewers
   usually respond within a few days to a couple of weeks. Expect at least one
   round of questions — that's normal, not a rejection.

6. **After acceptance**, Flathub creates
   `flathub/io.github.justlinuxnoob.ArchiveFree`. Add its
   `flathub.json` if you want specific architectures, and from then on
   publishing a new version means opening a PR there that bumps the `tag` and
   `commit`.

7. **Update the README** — remove the "submission pending" note and leave only
   the `flatpak install flathub io.github.justlinuxnoob.ArchiveFree` line.

## Optional, before submitting

The linter suggests GNOME runtime 50 (this manifest targets 49):

```
"warnings": ["runtime-update-available-to-org.gnome.Platform-50"]
```

This is advisory and will not block a submission — 49 is a supported stable
runtime. Bumping is a one-line change to `runtime-version`, but rebuild and
re-test locally first, since a newer runtime ships a newer GTK.

## Testing the manifest locally

```bash
flatpak install flathub org.flatpak.Builder
flatpak run org.flatpak.Builder --force-clean --repo=repo build-dir \
    packaging/flatpak/io.github.justlinuxnoob.ArchiveFree.yml
flatpak build-bundle repo archivefree.flatpak io.github.justlinuxnoob.ArchiveFree
flatpak install --user archivefree.flatpak
```

> **If the build fails at "Finishing app" with `Command 'archivefree' not
> found`** even though `build-dir/files/bin/archivefree` exists, your system's
> `rofiles-fuse` isn't working — common in containers and sandboxes. Add
> `--disable-rofiles-fuse`. This affects the build tool only, not the resulting
> Flatpak.

Run the linter the way Flathub will:

```bash
flatpak run --command=flatpak-builder-lint org.flatpak.Builder \
    manifest packaging/flatpak/io.github.justlinuxnoob.ArchiveFree.yml
flatpak run --command=flatpak-builder-lint org.flatpak.Builder repo repo
```
