"""The archive contents view: a sortable, searchable, navigable column view.

Navigation is folder-by-folder rather than a giant expandable tree. That keeps
the model tiny no matter how large the archive — we only ever build rows for the
folder currently on screen — and it matches how every file manager behaves, so
nobody has to learn anything.
"""

from __future__ import annotations

from collections.abc import Callable

from gi.repository import Gdk, Gio, GObject, Gtk

from ..core.tree import Node
from .utils import describe_type, format_count, format_date, format_size, icon_name


class RowItem(GObject.Object):
    """A GObject wrapper so a tree :class:`Node` can live in a Gio.ListModel."""

    __gtype_name__ = "ArchiveFreeRowItem"

    def __init__(self, node: Node, show_full_path: bool = False):
        super().__init__()
        self.node = node
        self.show_full_path = show_full_path

    @GObject.Property(type=str)
    def name(self) -> str:
        return self.node.path if self.show_full_path else self.node.name

    @GObject.Property(type=str)
    def sort_name(self) -> str:
        return self.node.name.lower()

    @GObject.Property(type=int)
    def size(self) -> int:
        return self.node.size

    @GObject.Property(type=bool, default=False)
    def is_dir(self) -> bool:
        return self.node.is_dir


class ArchiveBrowser(Gtk.Box):
    """Column view over one folder of an archive, plus recursive search results."""

    __gtype_name__ = "ArchiveFreeBrowser"

    def __init__(self, on_activate: Callable[[Node], None],
                 on_selection_changed: Callable[[], None],
                 on_drag_prepare: Callable[[Node], object] | None = None):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self._on_activate = on_activate
        self._on_selection_changed = on_selection_changed
        # Called with the row being dragged; returns a Gdk.ContentProvider.
        self._on_drag_prepare = on_drag_prepare

        self.root: Node | None = None
        self.current: Node | None = None
        self._search_text = ""

        self.store = Gio.ListStore(item_type=RowItem)
        self.selection = Gtk.MultiSelection(model=self._build_sort_model())
        self.selection.connect("selection-changed", lambda *_: on_selection_changed())

        self.column_view = Gtk.ColumnView(
            model=self.selection,
            show_column_separators=False,
            show_row_separators=False,
            reorderable=False,
            hexpand=True,
            vexpand=True,
        )
        self.column_view.add_css_class("af-contents")
        self.column_view.connect("activate", self._on_row_activated)
        self._add_columns()

        # Pressing a row collapses a multi-selection down to that row before the
        # drag source's "prepare" runs, so dragging three selected files would
        # only ever hand over one. Snapshot the selection on the way down, while
        # it is still intact, and let the event through untouched.
        self._selection_at_press: list[Node] = []
        watcher = Gtk.GestureClick(button=1)
        watcher.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        watcher.connect("pressed", self._snapshot_selection)
        self.column_view.add_controller(watcher)

        scroller = Gtk.ScrolledWindow(
            hscrollbar_policy=Gtk.PolicyType.AUTOMATIC,
            vscrollbar_policy=Gtk.PolicyType.AUTOMATIC,
            hexpand=True, vexpand=True,
        )
        scroller.set_child(self.column_view)
        self.append(scroller)

    def _snapshot_selection(self, _gesture, _n_press, _x, _y) -> None:
        self._selection_at_press = self.selected_nodes()

    # -- model plumbing --------------------------------------------------
    def _build_sort_model(self) -> Gtk.SortListModel:
        self.sort_model = Gtk.SortListModel(model=self.store)
        return self.sort_model

    def _add_columns(self) -> None:
        name_column = Gtk.ColumnViewColumn(title="Name", expand=True, resizable=True)
        name_column.set_factory(self._name_factory())
        name_column.set_sorter(self._folders_first_sorter())
        self.column_view.append_column(name_column)
        self._name_column = name_column

        # Fixed widths keep the metadata grouped together on a wide window
        # instead of stranding it against the right-hand edge.
        size_column = Gtk.ColumnViewColumn(title="Size", resizable=True,
                                           fixed_width=110)
        size_column.set_factory(self._text_factory(self._size_text, align=1.0,
                                                   dim=False, numeric=True))
        size_column.set_sorter(Gtk.NumericSorter(
            expression=Gtk.PropertyExpression.new(RowItem, None, "size")))
        self.column_view.append_column(size_column)

        packed_column = Gtk.ColumnViewColumn(title="Compressed", resizable=True,
                                             fixed_width=120)
        packed_column.set_factory(self._text_factory(self._packed_text, align=1.0,
                                                     dim=True, numeric=True))
        self.column_view.append_column(packed_column)
        self.packed_column = packed_column

        type_column = Gtk.ColumnViewColumn(title="Type", resizable=True,
                                           fixed_width=170)
        type_column.set_factory(self._text_factory(self._type_text, dim=True))
        self.column_view.append_column(type_column)
        self.type_column = type_column

        date_column = Gtk.ColumnViewColumn(title="Modified", resizable=True,
                                           fixed_width=140)
        date_column.set_factory(self._text_factory(self._date_text, dim=True))
        self.column_view.append_column(date_column)
        self.date_column = date_column

        # Sort by name ascending out of the box.
        self.column_view.sort_by_column(name_column, Gtk.SortType.ASCENDING)

    def _folders_first_sorter(self) -> Gtk.Sorter:
        """Folders above files, then case-insensitive by name."""
        multi = Gtk.MultiSorter()
        folder_sorter = Gtk.NumericSorter(
            expression=Gtk.PropertyExpression.new(RowItem, None, "is_dir"))
        folder_sorter.set_sort_order(Gtk.SortType.DESCENDING)
        multi.append(folder_sorter)
        multi.append(Gtk.StringSorter(
            expression=Gtk.PropertyExpression.new(RowItem, None, "sort_name")))
        return multi

    # -- cell factories --------------------------------------------------
    def _name_factory(self) -> Gtk.SignalListItemFactory:
        factory = Gtk.SignalListItemFactory()

        def setup(_f, list_item: Gtk.ListItem) -> None:
            box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
            icon = Gtk.Image(icon_size=Gtk.IconSize.NORMAL)
            label = Gtk.Label(xalign=0.0, ellipsize=3, hexpand=True)
            lock = Gtk.Image(icon_name="channel-secure-symbolic")
            lock.add_css_class("dim-label")
            lock.set_tooltip_text("Password-protected")
            lock.set_visible(False)
            box.append(icon)
            box.append(label)
            box.append(lock)
            list_item.set_child(box)

            # The drag source lives on the row, not on the ColumnView. Putting
            # it on the view makes it compete with the view's own selection
            # gesture and clicking stops selecting anything at all.
            if self._on_drag_prepare is not None:
                source = Gtk.DragSource(actions=Gdk.DragAction.COPY)
                source.connect("prepare", self._row_drag_prepare, box)
                source.connect("drag-begin", self._row_drag_begin, box)
                box.add_controller(source)

        def bind(_f, list_item: Gtk.ListItem) -> None:
            item: RowItem = list_item.get_item()
            box = list_item.get_child()
            icon, label, lock = (box.get_first_child(),
                                 box.get_first_child().get_next_sibling(),
                                 box.get_last_child())
            node = item.node
            icon.set_from_icon_name(icon_name(node.name, node.is_dir))
            icon.remove_css_class("af-folder-icon")
            if node.is_dir:
                icon.add_css_class("af-folder-icon")
            label.set_text(item.name)
            label.set_tooltip_text(node.path)
            lock.set_visible(bool(node.encrypted))
            # Remember which row this widget is showing, so its drag source
            # knows what it is dragging — rows are recycled while scrolling.
            box._af_node = node

        factory.connect("setup", setup)
        factory.connect("bind", bind)
        return factory

    def _row_drag_prepare(self, _source: Gtk.DragSource, _x: float, _y: float,
                          box: Gtk.Widget):
        node = getattr(box, "_af_node", None)
        if node is None or self._on_drag_prepare is None:
            return None
        return self._on_drag_prepare(node, self._selection_at_press)

    def _row_drag_begin(self, source: Gtk.DragSource, _drag, box: Gtk.Widget) -> None:
        node = getattr(box, "_af_node", None)
        if node is None:
            return
        # Dragging a row that isn't selected should drag that row, so make the
        # selection follow the cursor the way a file manager does.
        selected = {n.path for n in self.selected_nodes()}
        if node.path not in selected:
            position = self._position_of(node)
            if position is not None:
                self.selection.select_item(position, True)
        try:
            paintable = Gtk.WidgetPaintable.new(box)
            source.set_icon(paintable, 12, 12)
        except Exception:
            pass

    def _position_of(self, node: Node) -> int | None:
        for index in range(self.selection.get_n_items()):
            item = self.selection.get_item(index)
            if item is not None and item.node is node:
                return index
        return None

    def _text_factory(self, getter, align: float = 0.0, dim: bool = False,
                      numeric: bool = False) -> Gtk.SignalListItemFactory:
        factory = Gtk.SignalListItemFactory()

        def setup(_f, list_item: Gtk.ListItem) -> None:
            label = Gtk.Label(xalign=align, ellipsize=3, single_line_mode=True)
            if dim:
                label.add_css_class("dim-label")
            if numeric:
                label.add_css_class("numeric")
            list_item.set_child(label)

        def bind(_f, list_item: Gtk.ListItem) -> None:
            list_item.get_child().set_text(getter(list_item.get_item()))

        factory.connect("setup", setup)
        factory.connect("bind", bind)
        return factory

    @staticmethod
    def _size_text(item: RowItem) -> str:
        node = item.node
        if node.is_dir:
            return format_count(node.file_count)
        return format_size(node.size)

    @staticmethod
    def _packed_text(item: RowItem) -> str:
        node = item.node
        if node.is_dir or not node.compressed_size:
            return "—"
        return format_size(node.compressed_size)

    @staticmethod
    def _type_text(item: RowItem) -> str:
        return describe_type(item.node.name, item.node.is_dir)

    @staticmethod
    def _date_text(item: RowItem) -> str:
        return format_date(item.node.modified)

    # -- navigation ------------------------------------------------------
    def load(self, root: Node) -> None:
        self.root = root
        self.navigate_to(root)

    def navigate_to(self, node: Node) -> None:
        if not node.is_dir:
            return
        self.current = node
        self._search_text = ""
        self._populate(node.sorted_children)

    def navigate_up(self) -> bool:
        if self.current is None or self.current.parent is None:
            return False
        self.navigate_to(self.current.parent)
        return True

    def navigate_path(self, path: str) -> None:
        if self.root is None:
            return
        node = self.root.find(path)
        if node is not None and node.is_dir:
            self.navigate_to(node)

    def _populate(self, nodes, show_full_path: bool = False) -> None:
        self.store.remove_all()
        # splice() is a single model update; appending one by one would emit a
        # signal per row and visibly stutter on large folders.
        items = [RowItem(n, show_full_path) for n in nodes]
        if items:
            self.store.splice(0, 0, items)
        self._on_selection_changed()

    # -- search ----------------------------------------------------------
    def search(self, text: str) -> int:
        """Filter the whole archive, not just this folder. Returns match count."""
        self._search_text = text.strip().lower()
        if not self._search_text:
            if self.current:
                self._populate(self.current.sorted_children)
            return -1

        matches: list[Node] = []
        needle = self._search_text
        stack = [self.root] if self.root else []
        while stack:
            node = stack.pop()
            for child in node.children.values():
                if needle in child.name.lower():
                    matches.append(child)
                if child.is_dir:
                    stack.append(child)
        matches.sort(key=lambda n: (not n.is_dir, n.path.lower()))
        self._populate(matches, show_full_path=True)
        return len(matches)

    @property
    def searching(self) -> bool:
        return bool(self._search_text)

    # -- selection -------------------------------------------------------
    def selected_nodes(self) -> list[Node]:
        bitset = self.selection.get_selection()
        result: list[Node] = []
        for index in range(bitset.get_size()):
            item = self.selection.get_item(bitset.get_nth(index))
            if item is not None:
                result.append(item.node)
        return result

    def select_all(self) -> None:
        self.selection.select_all()

    def unselect_all(self) -> None:
        self.selection.unselect_all()

    @property
    def row_count(self) -> int:
        return self.store.get_n_items()

    def _on_row_activated(self, _view: Gtk.ColumnView, position: int) -> None:
        item = self.selection.get_item(position)
        if item is not None:
            self._on_activate(item.node)

    # -- column visibility (Preferences) ---------------------------------
    def set_column_visible(self, which: str, visible: bool) -> None:
        column = {
            "compressed": self.packed_column,
            "type": self.type_column,
            "modified": self.date_column,
        }.get(which)
        if column is not None:
            column.set_visible(visible)
