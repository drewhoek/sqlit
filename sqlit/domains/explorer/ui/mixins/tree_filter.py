"""Tree filter mixin for SSMSTUI."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from rich.markup import escape as escape_markup

from sqlit.shared.core.utils import fuzzy_match, highlight_matches
from sqlit.shared.ui.protocols import TreeFilterMixinHost

if TYPE_CHECKING:
    pass


@dataclass
class _NodeSnapshot:
    """Frozen capture of one tree node's state, used to restore the tree
    between filter keystrokes without calling refresh_tree.

    Calling refresh_tree drops lazy-loaded children (e.g. tables under a
    database in multi-DB browse mode) because the re-expand triggers an
    async reload that doesn't complete before the next filter pass — see
    issue #141.
    """

    label: Any
    data: Any
    allow_expand: bool
    is_expanded: bool
    children: list[_NodeSnapshot] = field(default_factory=list)


def _snapshot_node(node: Any) -> _NodeSnapshot:
    return _NodeSnapshot(
        label=node.label,
        data=node.data,
        allow_expand=getattr(node, "allow_expand", False),
        is_expanded=getattr(node, "is_expanded", False),
        children=[_snapshot_node(c) for c in node.children],
    )


def _restore_node_under(parent: Any, snap: _NodeSnapshot) -> None:
    child = parent.add(snap.label, data=snap.data)
    try:
        child.allow_expand = snap.allow_expand
    except Exception:
        pass
    if snap.is_expanded:
        try:
            child.expand()
        except Exception:
            pass
    for grandchild in snap.children:
        _restore_node_under(child, grandchild)


@dataclass
class _FilteredSnapshot:
    """A snapshot node that survived the filter: it matched, or a descendant did."""

    snap: _NodeSnapshot
    matched: bool
    indices: list[int]
    children: list[_FilteredSnapshot] = field(default_factory=list)


class TreeFilterMixin:
    """Mixin providing tree filter functionality."""

    _tree_filter_visible: bool = False
    _tree_filter_text: str = ""
    _tree_filter_query: str = ""
    _tree_filter_fuzzy: bool = False
    _tree_filter_typing: bool = False
    _tree_filter_matches: list[Any] = []
    _tree_filter_match_index: int = 0
    _tree_original_labels: dict[int, str] = {}
    _tree_snapshot: list[_NodeSnapshot] | None = None
    # True while the live tree shows a filtered subset rather than the snapshot.
    _tree_filtered_view: bool = False

    def action_tree_filter(self: TreeFilterMixinHost) -> None:
        """Open the tree filter."""
        if not self.object_tree.has_focus:
            self.object_tree.focus()

        self._tree_filter_visible = True
        self._tree_filter_text = ""
        self._tree_filter_query = ""
        self._tree_filter_fuzzy = False
        self._tree_filter_typing = True
        self._tree_filter_matches = []
        self._tree_filter_match_index = 0
        self._tree_original_labels = {}
        self._tree_filtered_view = False
        # Freeze the currently loaded tree (incl. lazy-loaded children)
        # so we can restore it between keystrokes without calling
        # refresh_tree, which would lose async-loaded folder contents.
        self._tree_snapshot = [_snapshot_node(c) for c in self.object_tree.root.children]

        self.tree_filter_input.show()
        self._update_tree_filter()
        self._update_footer_bindings()

    def action_tree_filter_close(self: TreeFilterMixinHost) -> None:
        """Close the tree filter and restore tree."""
        self._tree_filter_visible = False
        self._tree_filter_text = ""
        self._tree_filter_query = ""
        self._tree_filter_fuzzy = False
        self._tree_filter_typing = False
        self.tree_filter_input.hide()
        self._restore_tree_labels()
        if self._tree_filtered_view:
            self._restore_tree_from_snapshot()
        self._tree_snapshot = None
        self._update_footer_bindings()

    def action_tree_filter_accept(self: TreeFilterMixinHost) -> None:
        """Accept the highlighted row (or current match), close filter, and activate it."""
        # Prefer the row the user moved to with the arrow keys; fall back to
        # the current match. Remember the node's *data* (not the node
        # reference) before closing: closing the filter rebuilds the tree
        # from the snapshot taken at filter-open time, which replaces every
        # node object — so the reference we captured here would be stale
        # after close. The data payload, however, is the same object on both
        # old and new nodes (we pass it through unchanged in
        # _restore_node_under), so we can re-locate the match by identity.
        matched_data: Any = None
        cursor = getattr(self.object_tree, "cursor_node", None)
        if cursor is not None and cursor is not self.object_tree.root and getattr(cursor, "data", None):
            matched_data = cursor.data
        elif self._tree_filter_matches and self._tree_filter_match_index < len(self._tree_filter_matches):
            current_node = self._tree_filter_matches[self._tree_filter_match_index]
            if current_node and current_node.data:
                matched_data = current_node.data

        # Close the filter
        self.action_tree_filter_close()

        if matched_data is None:
            return

        fresh_node = self._find_node_by_data(matched_data)
        if fresh_node is None:
            return

        # Textual's Tree.move_cursor reads `node._line`, which is set during
        # the next layout pass — not when the node is `add()`-ed. Since the
        # snapshot restore that just ran in action_tree_filter_close added
        # all fresh nodes synchronously, calling move_cursor right now sees
        # stale `_line` values and parks the cursor on the wrong row.
        # Defer the move (and the activation) until after the next refresh.
        call_after = getattr(self, "call_after_refresh", None)
        if callable(call_after):
            call_after(lambda: self._select_and_activate_after_refresh(fresh_node))
        else:
            # Synchronous fallback (used in unit tests with a mock host).
            self._select_and_activate_after_refresh(fresh_node)

    def _select_and_activate_after_refresh(self: TreeFilterMixinHost, node: Any) -> None:
        # The snapshot restore put every ancestor back into its filter-open
        # state, which is often collapsed — the filter only expanded them in
        # the filtered view. Re-expand so the accepted node is actually
        # visible; expanding an already-populated node is a no-op in
        # on_tree_node_expanded, so this does not trigger a reload.
        self._expand_ancestors(node)
        self._move_cursor_to(node)
        self._activate_tree_node(node)

        # Mirror what Enter does on a highlighted node (Textual's toggle):
        # expand tables to their columns, folders to their contents,
        # databases to "use" them. Connections/saved queries are handled by
        # _activate_tree_node above.
        kind_getter = getattr(getattr(node, "data", None), "get_node_kind", None)
        kind = str(kind_getter()) if callable(kind_getter) else ""
        if kind in ("connection", "saved_query_file"):
            return
        if getattr(node, "allow_expand", False) and not getattr(node, "is_expanded", False):
            try:
                node.expand()
            except Exception:
                pass

    def _move_cursor_to(self: TreeFilterMixinHost, node: Any) -> None:
        """Move the tree cursor to `node`, making Textual assign line numbers first.

        Tree.move_cursor reads `node._line`, which is only set when the widget
        rebuilds its line list. Right after nodes were added (snapshot restore,
        filter apply) that value is stale and the cursor lands on row 0.
        Reading the `_tree_lines` property forces the rebuild synchronously.
        """
        try:
            getattr(self.object_tree, "_tree_lines", None)
        except Exception:
            pass
        try:
            self.object_tree.move_cursor(node)
        except Exception:
            pass

    def _find_node_by_data(self: TreeFilterMixinHost, data: Any) -> Any | None:
        """Locate the node in the current tree whose `.data` is `data`."""
        stack = [self.object_tree.root]
        while stack:
            node = stack.pop()
            if node.data is data:
                return node
            stack.extend(node.children)
        return None

    def action_tree_filter_next(self: TreeFilterMixinHost) -> None:
        """Move to next filter match."""
        if not self._tree_filter_matches:
            return
        self._tree_filter_match_index = (self._tree_filter_match_index + 1) % len(self._tree_filter_matches)
        self._jump_to_current_match()

    def action_tree_filter_prev(self: TreeFilterMixinHost) -> None:
        """Move to previous filter match."""
        if not self._tree_filter_matches:
            return
        self._tree_filter_match_index = (self._tree_filter_match_index - 1) % len(self._tree_filter_matches)
        self._jump_to_current_match()

    def _jump_to_current_match(self: TreeFilterMixinHost) -> None:
        """Jump to the current match in the tree."""
        if not self._tree_filter_matches:
            return
        node = self._tree_filter_matches[self._tree_filter_match_index]
        self._expand_ancestors(node)
        self._move_cursor_to(node)

    def _expand_ancestors(self: TreeFilterMixinHost, node: Any) -> None:
        """Expand all ancestor nodes to make a node visible."""
        ancestors = []
        current = node.parent
        while current and current != self.object_tree.root:
            ancestors.append(current)
            current = current.parent
        # Expand from root down
        for ancestor in reversed(ancestors):
            ancestor.expand()

    def on_key(self: TreeFilterMixinHost, event: Any) -> None:
        """Handle key events when tree filter is active."""
        if not self._tree_filter_visible:
            # Pass to next mixin in chain (e.g., AutocompleteMixin)
            super().on_key(event)  # type: ignore[misc]
            return

        key = event.key
        if key == "enter":
            self.action_tree_filter_accept()
            event.prevent_default()
            event.stop()
            return

        if not self._tree_filter_typing:
            if key in ("n", "j"):
                self.action_tree_filter_next()
                event.prevent_default()
                event.stop()
                return

            if key in ("N", "k"):
                self.action_tree_filter_prev()
                event.prevent_default()
                event.stop()
                return

            if key == "/":
                self.action_tree_filter()
                event.prevent_default()
                event.stop()
                return

        # Handle backspace
        if key == "backspace":
            if self._tree_filter_typing:
                if self._tree_filter_text:
                    self._tree_filter_text = self._tree_filter_text[:-1]
                    self._update_tree_filter()
                else:
                    # Exit filter when backspacing with no text
                    self.action_tree_filter_close()
            event.prevent_default()
            event.stop()
            return

        # Handle printable characters - use event.character for proper shift support
        # event.key might be "shift+?" but event.character will be "?"
        char = getattr(event, "character", None)
        if char and char.isprintable():
            if char == "/" and not self._tree_filter_typing:
                self.action_tree_filter()
                event.prevent_default()
                event.stop()
                return
            if not self._tree_filter_typing:
                super().on_key(event)  # type: ignore[misc]
                return
            self._tree_filter_text += char
            self._update_tree_filter()
            event.prevent_default()
            event.stop()
            return

        # Pass unhandled keys to next mixin
        super().on_key(event)  # type: ignore[misc]

    def _update_tree_filter(self: TreeFilterMixinHost) -> None:
        """Rebuild the tree from the snapshot showing only matches and their ancestors.

        Every pass searches the snapshot taken at filter open (so backspacing
        widens again — PR #211 — and lazy-loaded children survive — issue
        #141) and materialises only the surviving nodes, each ancestor
        expanded so no match is hidden. Filtering the live tree instead, by
        restoring every node and removing the non-matching ones one by one,
        made each keystroke O(n²) in Textual (TreeNode.remove is
        O(siblings)); a library with thousands of tables stalled for seconds
        per character.
        """
        self._tree_original_labels = {}
        raw_text = self._tree_filter_text
        self._tree_filter_fuzzy = raw_text.startswith("~")
        self._tree_filter_query = raw_text[1:] if self._tree_filter_fuzzy else raw_text

        if self._tree_snapshot is None:
            self._tree_snapshot = [_snapshot_node(c) for c in self.object_tree.root.children]
        snapshot = self._tree_snapshot
        total = self._count_snapshot_nodes(snapshot)

        if not self._tree_filter_query:
            # Only rebuild if a filtered subset is showing; on open (and after
            # backspacing to empty) the live tree may already be the snapshot.
            if self._tree_filtered_view:
                self._restore_tree_from_snapshot()
            self._tree_filter_matches = []
            self.tree_filter_input.set_filter("", 0, total)
            return

        survivors = [entry for entry in (self._filter_snapshot(snap) for snap in snapshot) if entry is not None]

        self._clear_tree()
        theme = getattr(self, "current_theme", None)
        style = f"bold {getattr(theme, 'primary', '#1565C0')}"
        matches: list[Any] = []
        for entry in survivors:
            self._materialize_filtered(self.object_tree.root, entry, matches, style)
        self._tree_filtered_view = True

        self._tree_filter_matches = matches
        self._tree_filter_match_index = 0
        self.tree_filter_input.set_filter(self._tree_filter_text, len(matches), total)

        if matches:
            self._jump_to_current_match()

    def _clear_tree(self: TreeFilterMixinHost) -> None:
        """Drop every node under the root.

        Textual's Tree.clear() swaps in a fresh root in one step. Removing
        nodes individually costs a widget refresh per node (and TreeNode._remove
        is O(siblings)), which on a library holding thousands of tables turned
        the first keystroke into a multi-second stall. Callers must re-read
        `object_tree.root` afterwards. The per-node path is kept for hosts
        without clear() (the unit-test mock).
        """
        clear = getattr(self.object_tree, "clear", None)
        if callable(clear):
            try:
                clear()
                return
            except Exception:
                pass

        def remove_subtree(node: Any) -> None:
            for child in list(node.children):
                remove_subtree(child)
                try:
                    child.remove()
                except Exception:
                    pass

        remove_subtree(self.object_tree.root)

    def _count_snapshot_nodes(self: TreeFilterMixinHost, snapshot: list[_NodeSnapshot]) -> int:
        """Count searchable nodes (those with data and a label) in the snapshot."""
        count = 0
        stack = list(snapshot)
        while stack:
            snap = stack.pop()
            if snap.data and self._label_text_of(snap.data):
                count += 1
            stack.extend(snap.children)
        return count

    def _match_label(self: TreeFilterMixinHost, label_text: str) -> tuple[bool, list[int]]:
        """Return (matched, matched character indices) for the current query."""
        if not label_text:
            return False, []
        if self._tree_filter_fuzzy:
            return fuzzy_match(self._tree_filter_query, label_text)
        start = label_text.lower().find(self._tree_filter_query.lower())
        if start < 0:
            return False, []
        return True, list(range(start, start + len(self._tree_filter_query)))

    def _filter_snapshot(self: TreeFilterMixinHost, snap: _NodeSnapshot) -> _FilteredSnapshot | None:
        """Prune the snapshot to nodes that match or have a matching descendant."""
        matched, indices = self._match_label(self._label_text_of(snap.data))
        children = [entry for entry in (self._filter_snapshot(c) for c in snap.children) if entry is not None]
        if not matched and not children:
            return None
        return _FilteredSnapshot(snap, matched, indices, children)

    def _materialize_filtered(
        self: TreeFilterMixinHost,
        parent: Any,
        entry: _FilteredSnapshot,
        matches: list[Any],
        style: str,
    ) -> None:
        """Add a filtered snapshot entry under `parent`, highlighted and expanded."""
        snap = entry.snap
        label: Any = snap.label
        if entry.matched:
            label = self._highlight_label(str(snap.label), self._label_text_of(snap.data), entry.indices, style)
        node = parent.add(label, data=snap.data)
        try:
            node.allow_expand = snap.allow_expand
        except Exception:
            pass
        if entry.matched:
            matches.append(node)
        for child in entry.children:
            self._materialize_filtered(node, child, matches, style)
        if entry.children:
            try:
                node.expand()
            except Exception:
                pass

    def _get_node_label_text(self, node: Any) -> str:
        """Get the plain text label for a node."""
        return self._label_text_of(node.data)

    def _label_text_of(self, data: Any) -> str:
        """Get the searchable plain text for a node's data payload."""
        if data is None:
            return ""
        label_getter = getattr(data, "get_label_text", None)
        if callable(label_getter):
            value = label_getter()
            if isinstance(value, str):
                return value
            return "" if value is None else str(value)
        return ""

    def _highlight_label(self, displayed: str, label_text: str, indices: list[int], style: str) -> str:
        """Highlight the match inside the label as displayed.

        The searchable text (`data.get_label_text()`) is often not the
        displayed label: a folder's text is "procedures" while it renders as
        "Stored Procedures". Highlighting the raw text used to replace the
        display label wholesale. Locate the query (or the raw text) inside the
        displayed label and highlight there; fall back to the raw text only if
        neither is a substring.
        """
        escaped = escape_markup(displayed)
        if not self._tree_filter_fuzzy and self._tree_filter_query:
            pos = escaped.lower().find(self._tree_filter_query.lower())
            if pos >= 0:
                span = list(range(pos, pos + len(self._tree_filter_query)))
                return highlight_matches(escaped, span, style=style)
        offset = escaped.lower().find(escape_markup(label_text).lower())
        if offset >= 0:
            return highlight_matches(escaped, [i + offset for i in indices], style=style)
        return highlight_matches(escape_markup(label_text), indices, style=style)

    def _show_all_tree_nodes(self: TreeFilterMixinHost) -> None:
        """Rebuild the tree to restore all nodes after filtering."""
        if self._tree_snapshot is not None:
            self._restore_tree_from_snapshot()
        else:
            # Fallback for paths that aren't inside an open filter session.
            self.refresh_tree()

    def _restore_tree_from_snapshot(self: TreeFilterMixinHost) -> None:
        """Rebuild the root's children from the snapshot taken at filter open."""
        snapshot = self._tree_snapshot
        if snapshot is None:
            return
        self._clear_tree()
        root = self.object_tree.root  # Tree.clear() replaces the root node
        for snap in snapshot:
            _restore_node_under(root, snap)
        self._tree_filtered_view = False

    def _restore_tree_labels(self: TreeFilterMixinHost) -> None:
        """Restore original labels for all modified nodes."""

        def restore_node(node: Any) -> None:
            node_id = id(node)
            if node_id in self._tree_original_labels:
                node.set_label(self._tree_original_labels[node_id])
            for child in node.children:
                restore_node(child)

        restore_node(self.object_tree.root)
        self._tree_original_labels = {}

    def _count_all_nodes(self: TreeFilterMixinHost) -> int:
        """Count all searchable nodes in the tree."""
        count = 0

        def count_nodes(node: Any) -> None:
            nonlocal count
            if node.data and self._get_node_label_text(node):
                count += 1
            for child in node.children:
                count_nodes(child)

        count_nodes(self.object_tree.root)
        return count
