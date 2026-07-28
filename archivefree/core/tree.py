"""Turning a flat entry list into a browsable folder tree.

Archives store a flat list of paths. Some record their directories explicitly,
some don't, and some do it inconsistently — so we synthesise every intermediate
folder rather than trusting the archive to have listed them. Folder sizes are
rolled up from their children so the browse view can show a meaningful figure.
"""

from __future__ import annotations

import posixpath
from dataclasses import dataclass, field

from .entry import ArchiveEntry


@dataclass
class Node:
    """One row in the browse view: a file, or a folder with children."""

    name: str
    path: str
    is_dir: bool
    entry: ArchiveEntry | None = None
    children: dict[str, Node] = field(default_factory=dict)
    parent: Node | None = None

    # Rolled-up figures, filled in by :func:`_summarise`.
    size: int = 0
    compressed_size: int = 0
    file_count: int = 0
    modified: object | None = None
    encrypted: bool = False

    @property
    def sorted_children(self) -> list[Node]:
        """Folders first, then files, each alphabetically — the desktop convention."""
        return sorted(self.children.values(), key=lambda n: (not n.is_dir, n.name.lower()))

    def child(self, name: str) -> Node | None:
        return self.children.get(name)

    def find(self, path: str) -> Node | None:
        node: Node | None = self
        if not path:
            return self
        for part in path.split("/"):
            if node is None:
                return None
            node = node.children.get(part)
        return node

    def walk_files(self):
        """Yield every non-directory entry beneath this node."""
        if not self.is_dir:
            if self.entry is not None:
                yield self.entry
            return
        for child in self.children.values():
            yield from child.walk_files()

    def all_entries(self):
        """Yield this node's entry (if any) and every entry beneath it."""
        if self.entry is not None:
            yield self.entry
        for child in self.children.values():
            yield from child.all_entries()


def build_tree(entries: list[ArchiveEntry]) -> Node:
    """Build the folder tree. Returns the (unnamed) root node."""
    root = Node(name="", path="", is_dir=True)

    for entry in entries:
        if not entry.path:
            continue
        parts = entry.path.split("/")
        node = root
        # Create intermediate folders, whether or not the archive listed them.
        for depth, part in enumerate(parts[:-1]):
            existing = node.children.get(part)
            if existing is None:
                existing = Node(
                    name=part,
                    path="/".join(parts[: depth + 1]),
                    is_dir=True,
                    parent=node,
                )
                node.children[part] = existing
            elif not existing.is_dir:
                # A file and a folder share a name: promote it to a folder so
                # the children aren't silently dropped.
                existing.is_dir = True
            node = existing

        leaf_name = parts[-1]
        leaf = node.children.get(leaf_name)
        if leaf is None:
            leaf = Node(name=leaf_name, path=entry.path, is_dir=entry.is_dir, parent=node)
            node.children[leaf_name] = leaf
        # A directory entry listed after its children keeps its children.
        if entry.is_dir:
            leaf.is_dir = True
        leaf.entry = entry

    _summarise(root)
    return root


def _summarise(node: Node) -> None:
    """Roll child sizes, counts and dates up into their parent folders."""
    if not node.is_dir:
        entry = node.entry
        if entry is not None:
            node.size = max(entry.size, 0)
            node.compressed_size = entry.compressed_size
            node.modified = entry.modified
            node.encrypted = entry.encrypted
        node.file_count = 1
        return

    total = compressed = count = 0
    newest = None
    encrypted = False
    for child in node.children.values():
        _summarise(child)
        total += child.size
        compressed += child.compressed_size
        count += child.file_count
        encrypted = encrypted or child.encrypted
        if child.modified is not None and (newest is None or child.modified > newest):
            newest = child.modified

    node.size = total
    node.compressed_size = compressed
    node.file_count = count
    node.encrypted = encrypted
    if node.entry is not None and node.entry.modified is not None:
        node.modified = node.entry.modified
    else:
        node.modified = newest


def common_root(entries: list[ArchiveEntry]) -> str | None:
    """The single top-level folder every entry sits under, if there is one.

    Used to avoid "tarbombs": if an archive already wraps its contents in one
    folder, extracting it doesn't need another wrapper, and if it doesn't, the
    UI offers to add one.
    """
    top: set[str] = set()
    for entry in entries:
        head = entry.path.split("/", 1)[0]
        if head:
            top.add(head)
        if len(top) > 1:
            return None
    if len(top) != 1:
        return None
    only = next(iter(top))
    # It's only a real root if something actually lives inside it.
    if any(e.path.startswith(only + "/") for e in entries):
        return only
    return None


def suggested_folder_name(archive_path: str) -> str:
    """Pick the folder name to extract into: "photos.tar.gz" -> "photos"."""
    name = posixpath.basename(archive_path)
    lowered = name.lower()
    from . import detect

    for ext, _ in sorted(
        ((e, k) for k in detect.FORMATS for e in detect.FORMATS[k].extensions),
        key=lambda pair: -len(pair[0]),
    ):
        if lowered.endswith(ext):
            return name[: -len(ext)] or name
    stem, _, _ = name.rpartition(".")
    return stem or name
