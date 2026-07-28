"""GTK 4 user interface.

The GI versions are pinned here rather than in each module. Importing a
namespace without pinning it first lets PyGObject pick whatever version it
finds, which on a machine with both GTK 3 and GTK 4 installed can silently load
the wrong one — and any module in this package may be imported first, whether by
the application or by a test.
"""

from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("Adw", "1")
