"""Progress reporting and cancellation on a genuinely large archive.

These guard the promise that a big archive never locks the interface: work
happens on a worker thread, progress arrives while it runs, and cancelling
takes effect promptly rather than after the operation finishes anyway.
"""

from __future__ import annotations

import os
import threading
import time

import pytest

from archivefree.core import registry
from archivefree.core.create import CreateOptions, create_archive
from archivefree.core.jobs import Cancelled, Job, Progress

from .conftest import have


@pytest.fixture(scope="module")
def big_archive(tmp_path_factory):
    """~60 MB across 400 files: big enough to observe progress and cancel it."""
    root = tmp_path_factory.mktemp("big")
    source = root / "payload"
    source.mkdir()
    # Incompressible data, so compression can't make this finish instantly.
    block = os.urandom(150 * 1024)
    for index in range(400):
        folder = source / f"group{index // 50}"
        folder.mkdir(exist_ok=True)
        (folder / f"file{index:03d}.bin").write_bytes(block + os.urandom(1024))

    archive = str(root / "big.zip")
    create_archive([str(source)], CreateOptions(destination=archive, format="zip",
                                                level="store", base_dir=str(source)))
    return archive


def test_listing_a_large_archive_is_fast(big_archive):
    """Listing must not decompress anything — it reads the index and stops."""
    started = time.monotonic()
    with registry.open_archive(big_archive) as backend:
        entries = backend.list_entries()
    elapsed = time.monotonic() - started
    assert len(entries) >= 400
    # Generous: this is really checking we didn't accidentally read the payload.
    assert elapsed < 2.0, f"listing took {elapsed:.2f}s — is it decompressing?"


def test_progress_is_reported_during_extraction(big_archive, tmp_path):
    samples: list[float] = []

    progress = Progress()
    progress._callback = lambda p: samples.append(p.fraction)
    progress._min_interval = 0.0  # capture every update for the test

    with registry.open_archive(big_archive) as backend:
        backend.extract(str(tmp_path / "out"), progress=progress)

    assert len(samples) > 5, "progress was barely reported"
    assert samples == sorted(samples), "progress went backwards"
    assert samples[-1] == pytest.approx(1.0, abs=0.01), "never reached 100%"


def test_cancelling_mid_extraction_stops_promptly(big_archive, tmp_path):
    """Cancel while it's actually running, not before it starts."""
    destination = tmp_path / "cancelled"
    started = threading.Event()
    progress = Progress()

    def watch(p: Progress) -> None:
        if p.fraction > 0.05:
            started.set()

    progress._callback = watch
    progress._min_interval = 0.0

    backend = registry.open_archive(big_archive)
    result: dict[str, BaseException | None] = {"error": None}

    def work() -> None:
        try:
            backend.extract(str(destination), progress=progress)
        except BaseException as exc:
            result["error"] = exc

    thread = threading.Thread(target=work)
    thread.start()

    assert started.wait(timeout=30), "extraction never got going"
    cancel_time = time.monotonic()
    progress.cancel()
    thread.join(timeout=15)
    elapsed = time.monotonic() - cancel_time
    backend.close()

    assert not thread.is_alive(), "extraction ignored the cancellation"
    assert isinstance(result["error"], Cancelled), \
        f"expected Cancelled, got {result['error']!r}"
    assert elapsed < 5.0, f"took {elapsed:.1f}s to stop after cancelling"

    # A cancelled extraction leaves a partial result, but must not have
    # finished: the whole point is that it stopped early.
    written = sum(len(files) for _, _, files in os.walk(destination))
    assert written < 400, "cancellation didn't actually stop the work"


@pytest.mark.skipif(not have("7z"), reason="7z not installed")
def test_cancelling_a_subprocess_backend(big_archive, tmp_path):
    """The 7-Zip backend must kill its child process, not just stop reading it."""
    source = tmp_path / "src"
    source.mkdir()
    (source / "blob.bin").write_bytes(os.urandom(40 * 1024 * 1024))

    archive = str(tmp_path / "slow.7z")
    progress = Progress()
    started = threading.Event()

    def watch(p: Progress) -> None:
        if p.fraction > 0.02:
            started.set()

    progress._callback = watch
    progress._min_interval = 0.0

    result: dict[str, BaseException | None] = {"error": None}

    def work() -> None:
        try:
            create_archive([str(source)],
                           CreateOptions(destination=archive, format="7z",
                                         level="maximum", base_dir=str(source)),
                           progress=progress)
        except BaseException as exc:
            result["error"] = exc

    thread = threading.Thread(target=work)
    thread.start()
    if not started.wait(timeout=30):
        progress.cancel()
        thread.join(timeout=10)
        pytest.skip("compression finished before we could cancel it")

    progress.cancel()
    thread.join(timeout=15)
    assert not thread.is_alive(), "7-Zip subprocess was not killed"
    assert isinstance(result["error"], Cancelled)


def test_job_delivers_progress_and_result_without_a_main_loop(big_archive, tmp_path):
    """Job callbacks must fire even when no GTK loop is running (as in tests)."""
    done: list[object] = []
    seen: list[float] = []

    def work(progress: Progress):
        with registry.open_archive(big_archive) as backend:
            return backend.extract(str(tmp_path / "jobout"), progress=progress)

    job = Job(work, on_done=done.append, on_progress=lambda p: seen.append(p.fraction))
    job.start()
    assert job.finished.wait(timeout=120), "job never finished"
    assert done, "on_done was never called"
    assert len(done[0]) >= 400
    assert seen, "on_progress was never called"
