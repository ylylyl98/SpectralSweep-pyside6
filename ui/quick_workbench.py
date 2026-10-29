"""Quick workspaces and command surfaces for the shared desktop runtime.

Quick editors and scientific plots retain their domain owners through explicit
compatibility adapters. View replacement never constructs/disconnects devices.
"""
from __future__ import annotations

from PySide6.QtCore import QObject, Property, Signal, Slot, QTimer, QUrl, Qt, QEvent, QPoint
from PySide6.QtGui import QDesktopServices
from PySide6.QtQuickWidgets import QQuickWidget
from ui.quick_engine import QuickSurface
from PySide6.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout, QDialog, QScrollArea, QStackedWidget, QSplitter, QSizePolicy
from shiboken6 import isValid

from app.ui_runtime import RuntimeState
from ui.quick_controls import QML_ROOT, install_spectrum_controls, install_workflow_controls


class NavigationItem(QObject):
    changed = Signal()

    def __init__(self, page_id, title, parent=None):
        super().__init__(parent)
        self._id, self._title = page_id, title
        self._enabled, self._visible, self._selected = True, True, False

    pageId = Property(str, lambda self: self._id, constant=True)
    title = Property(str, lambda self: self._title, constant=True)
    enabled = Property(bool, lambda self: self._enabled, notify=changed)
    visible = Property(bool, lambda self: self._visible, notify=changed)
    selected = Property(bool, lambda self: self._selected, notify=changed)

    def update(self, enabled, visible, selected):
        state = bool(enabled), bool(visible), bool(selected)
        if state != (self._enabled, self._visible, self._selected):
            self._enabled, self._visible, self._selected = state
            self.changed.emit()


class QuickWorkbench(QObject):
    changed = Signal()
    pagesChanged = Signal()
    sampleEditorChanged = Signal()

    def __init__(self, window):
        super().__init__(window)
        from PySide6.QtWidgets import QApplication
        app = QApplication.instance()
        if app.property("spectralQuick"):
            from ui.quick_style import apply_quick_style
            apply_quick_style(app, app.property("quickThemeMode") or "system")
        self.window = window
        self.runtime = RuntimeState(self)
        self._pages = []
        self._commands = {}
        self._page_editors = {}
        self._sidebar_forms = None
        self._theme_mode = app.property("quickThemeMode") or "system"
        self._history_dialog = None
        self._history_widget = None
        self._history_context = None
        self._last_sample = None
        self._closed = False
        self._sample_source_edit = None
        self._sample_source_binder = None
        self._sample_editor_state = None
        self._dispatching = set()
        from ui.quick_dialogs import install_dialog_migration
        self._dialog_migration = install_dialog_migration(window)
        for panel, page_id in window._tab_ids.items():
            index = window._tabs.indexOf(panel)
            title = window._tabs.tabText(index)
            self.runtime.register_page(page_id, title,
                                       resources=() if page_id in {"settings", "calibration"} else ("instruments",))
            self._pages.append(NavigationItem(page_id, title, self))
        self.runtime.changed.connect(self.changed)
        self.navigation = QuickSurface(window)
        self.navigation.setResizeMode(QQuickWidget.ResizeMode.SizeRootObjectToView)
        self.navigation.setInitialProperties({"bridge": self})
        self.navigation.setSource(QUrl.fromLocalFile(str(QML_ROOT / "Navigation.qml")))
        if self.navigation.status() == QQuickWidget.Status.Error:
            raise RuntimeError("\n".join(e.toString() for e in self.navigation.errors()))
        self._navigation_height_timer = QTimer(self)
        self._navigation_height_timer.setSingleShot(True)
        self._navigation_height_timer.timeout.connect(self._resize_navigation)
        self.navigation.rootObject().implicitHeightChanged.connect(self._schedule_navigation_height)
        self._resize_navigation()
        self._install_commands()
        for page_id, widget in self._commands.items():
            self._bind_admission(page_id, widget)
        central = window.takeCentralWidget()
        self._content_splitter = getattr(window, "_sidebar_splitter", central if isinstance(central, QSplitter) else None)
        self._instrument_focus = False
        self._narrow = None
        self._wide_sidebar_width = getattr(window, "_sidebar_width", 300)
        # Older layouts used 300/330 px even on a full HD display. Let those
        # defaults follow the viewport until the user moves the splitter.
        self._auto_sidebar_width = self._wide_sidebar_width <= 330
        if self._content_splitter is not None and self._content_splitter.count():
            self._content_splitter.splitterMoved.connect(self._remember_wide_sidebar_width)
            sidebar = self._content_splitter.widget(0)
            sidebar.setProperty("quickSidebar", True)
            sidebar.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
            sidebar.style().unpolish(sidebar)
            sidebar.style().polish(sidebar)
        self.command_stack = QStackedWidget(window)
        self.command_stack.setObjectName("QuickPageHeaderCommands")
        self.command_stack.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        for widget in self._commands.values():
            self._mount_command(widget)
        wrapper = QWidget(window)
        layout = QVBoxLayout(wrapper)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self.navigation)
        self.command_header = QWidget(wrapper)
        self.command_header.setObjectName("QuickPageCommandHeader")
        header_layout = QHBoxLayout(self.command_header)
        header_layout.setContentsMargins(0, 0, 6, 0)
        header_layout.addStretch(1)
        header_layout.addWidget(self.command_stack)
        layout.addWidget(self.command_header)
        # A workspace's complex editor may need more height than a laptop
        # viewport. Scroll its content, keeping navigation and Stop visible.
        self.body_scroll = QScrollArea(wrapper)
        self.body_scroll.setObjectName("QuickWorkspaceViewport")
        self.body_scroll.setWidgetResizable(True)
        self.body_scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        self.body_scroll.setWidget(central)
        layout.addWidget(self.body_scroll, 1)
        self.body_scroll.verticalScrollBar().valueChanged.connect(self.refresh)
        window.setCentralWidget(wrapper)
        window.installEventFilter(self)
        window._tabs.tabBar().hide()
        # QTabWidget aggregates every hidden page's minimum height, including
        # its hidden tab bar. Size this host from the selected workspace only.
        window._tabs.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Ignored)
        if self._content_splitter is not None and self._content_splitter.count() >= 2:
            # setSizePolicy above resets the tab host's stretch. Restore it
            # afterwards so window resizing gives its extra width to the page.
            self._content_splitter.setStretchFactor(0, 0)
            self._content_splitter.setStretchFactor(1, 1)
        window._tabs.currentChanged.connect(self.refresh)
        window._history_type.currentTextChanged.connect(self._history_kind_changed)
        window._status.messageChanged.connect(self.changed)
        self._timer = QTimer(self)
        self._timer.setInterval(200)
        self._timer.timeout.connect(self.refresh)
        self._timer.start()
        self.refresh()

    def _mount_command(self, widget):
        old_parent = widget.parentWidget()
        if old_parent is not None and old_parent.layout() is not None:
            old_parent.layout().removeWidget(widget)
        self.command_stack.addWidget(widget)
        widget.set_docked(compact=True)
        if not getattr(widget, "_workbench_command_mounted", False):
            widget.heightHintChanged.connect(self._resize_commands)
            widget._workbench_command_mounted = True

    def _resize_commands(self):
        widget = self.command_stack.currentWidget()
        if widget is not None:
            self.command_stack.setFixedHeight(widget.height())
            self.command_stack.setMaximumWidth(widget.maximumWidth())
            self.command_header.setFixedHeight(widget.height())

    def _command_pane(self, panel, page_id):
        if page_id == "spectrum":
            splitter = getattr(panel, "_content_splitter", None)
            if not isinstance(splitter, QSplitter) or splitter.count() < 2:
                return None
            return self._wrap_command_pane(panel, splitter)
        if page_id == "dual_gate":
            return getattr(panel, "_results_pane", None)
        pane = getattr(panel, "_right_panel", None)
        if pane is not None:
            return pane
        splitter = getattr(panel, "_splitter", None)
        if not isinstance(splitter, QSplitter) or splitter.count() < 2:
            return None
        pane = splitter.widget(1)
        if isinstance(pane.layout(), QVBoxLayout):
            return pane
        return self._wrap_command_pane(panel, splitter)

    def _wrap_command_pane(self, panel, splitter):
        # Spectrum's existing reference/current-acquisition splitter and BFP's
        # existing tab view retain their owners inside the same splitter slot.
        # Only the action area stays fixed below that scrollable/view content.
        wrapper = getattr(panel, "_quick_command_pane", None)
        if wrapper is None:
            pane = splitter.widget(1)
            was_hidden = pane.isHidden()
            sizes = splitter.sizes()
            wrapper = QWidget(splitter)
            wrapper.setObjectName("QuickMonitoringCommandPane")
            pane_layout = QVBoxLayout(wrapper)
            pane_layout.setContentsMargins(0, 0, 0, 0)
            pane_layout.setSpacing(2)
            pane_layout.setAlignment(Qt.AlignmentFlag.AlignBottom)
            pane_layout.addWidget(pane, 1)
            pane.setVisible(not was_hidden)
            splitter.insertWidget(1, wrapper)
            splitter.setSizes(sizes)
            panel._quick_command_pane = wrapper
        return wrapper

    def _relocate_output_actions(self, panel, page_id):
        """Keep BFP's existing output controls beside its acquisition actions."""
        if page_id != "bfp" or hasattr(panel, "_quick_output_actions"):
            return
        sources = [getattr(panel, name, None) for name in
                   ("_clear_btn", "_save_csv_btn", "_save_png_btn", "_status_lbl")]
        if not all(isinstance(source, QWidget) for source in sources):
            return

        def contains(layout, source):
            return any(item.widget() is source or
                       (item.layout() is not None and contains(item.layout(), source))
                       for item in (layout.itemAt(i) for i in range(layout.count())))

        root = panel.layout()
        footer_index = next((index for index in range(root.count())
                             if root.itemAt(index).layout() is not None
                             and all(contains(root.itemAt(index).layout(), source)
                                     for source in sources)), None)
        pane = self._command_pane(panel, page_id)
        if footer_index is None or pane is None:
            return
        footer = root.takeAt(footer_index).layout()

        def widgets(layout):
            for index in range(layout.count()):
                item = layout.itemAt(index)
                if item.widget() is not None:
                    yield item.widget()
                elif item.layout() is not None:
                    yield from widgets(item.layout())

        hidden = {source: source.isHidden() for source in widgets(footer)}
        footer.setParent(None)
        holder = QWidget(pane)
        holder.setObjectName("QuickBfpOutputActions")
        holder.setProperty("quickSectionBoundary", True)
        holder.setLayout(footer)
        footer.setContentsMargins(8, 0, 8, 0)
        holder.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        pane.layout().addWidget(holder)
        for source, was_hidden in hidden.items():
            source.setVisible(not was_hidden)
        holder.show()
        panel._quick_output_actions = holder

    def _dock_page_commands(self, panel, page_id):
        """Dock actions at the lower right, matching the Dual Gate workspace."""
        command = self._commands.get(page_id)
        if command is None:
            return False
        pane = self._command_pane(panel, page_id) if panel is not None else None
        use_header = self._instrument_focus and page_id in {"mcd", "mcd2100"}
        if isinstance(pane, QWidget) and not use_header:
            # A running measurement's Stop must remain reachable when the
            # outer laptop viewport scrolls the lower action area off screen.
            safety_active = any(button.isEnabled() for key, button in command.adapter.buttons.items()
                                if key in {"stop", "abort"})
            top = pane.mapTo(self.body_scroll.viewport(), QPoint()).y()
            bottom = top + pane.height()
            use_header = safety_active and (
                bottom - command.height() < -4
                or bottom > self.body_scroll.viewport().height() + 4)
        if use_header:
            pane = None
        if command is None or not isinstance(pane, QWidget) or pane.layout() is None:
            if command is not None and command.parentWidget() is not self.command_stack:
                self._mount_command(command)
            return False
        # A responsive source page can append optional content after a dock
        # (for example Mega Sweep's optical sequence). Keep actions last.
        if command.parentWidget() is not pane or pane.layout().indexOf(command) != pane.layout().count() - 1:
            old_parent = command.parentWidget()
            if old_parent is not None and old_parent.layout() is not None:
                old_parent.layout().removeWidget(command)
            pane.layout().addWidget(command, 0, Qt.AlignmentFlag.AlignRight)
            command.set_docked(compact=True)
            command.show()
        return True

    def eventFilter(self, watched, event):
        if watched is self.window and event.type() == QEvent.Type.Resize and not self._closed:
            QTimer.singleShot(0, self, self._fit_workspace)
        return False

    def _remember_wide_sidebar_width(self, *_args):
        splitter = self._content_splitter
        if (self._narrow is False and not self._instrument_focus
                and splitter is not None and splitter.widget(0).isVisible()):
            self._auto_sidebar_width = False
            width = splitter.sizes()[0]
            if width > 16:
                self._wide_sidebar_width = width
                self.window._sidebar_width = width

    def _fit_workspace(self):
        splitter = self._content_splitter
        if splitter is None or splitter.count() < 2:
            return
        narrow = self.window.width() < 1200
        changed = narrow != self._narrow
        if changed:
            self._narrow = narrow
            self._instrument_focus = False
            self.window._tabs.show()
            splitter.widget(0).setVisible(not narrow)
        if narrow:
            return
        automatic = getattr(self, "_auto_sidebar_width", False)
        width = (max(350, min(460, round(self.window.width() * .24)))
                 if automatic else self._wide_sidebar_width)
        width = max(splitter.widget(0).minimumWidth(), width)
        if automatic:
            # Respect the selected page's current limits, including transient
            # startup geometry, without retrying an unattainable size forever.
            width = splitter.closestLegalPosition(width, 1)
        # setSizes(0, ...) is also used by the existing Hide action. Do not
        # reopen a collapsed/hidden pane during a refresh or a wide resize.
        expanded = splitter.widget(0).isVisible() and splitter.sizes()[0] > 16
        if changed or (automatic and expanded and splitter.sizes()[0] != width):
            self._wide_sidebar_width = width
            self.window._sidebar_width = width
            splitter.setSizes([width, max(1, splitter.width() - splitter.handleWidth() - width)])

    def _show_workspace(self):
        if self._instrument_focus and self._content_splitter is not None:
            self._instrument_focus = False
            self._content_splitter.widget(0).hide()
            self.window._tabs.show()
            action = getattr(self.window, "_sidebar_toggle_action", None)
            if action is not None:
                action.setText("Show sidebar")

    def _ensure_page_editors(self, panel, page_id):
        if page_id in self._page_editors:
            return
        self._page_editors[page_id] = self._build_page_editors(panel, page_id)

    def _build_page_editors(self, panel, page_id):
        """Build an isolated page without replacing the currently mounted one."""
        from ui.quick_table_editor import install_table_editors
        from ui.quick_parameter_forms import install_parameter_forms
        references = None
        if page_id == "spectrum" and hasattr(panel, "_reference_list"):
            from ui.quick_reference_list import install_reference_list
            references = install_reference_list(panel)
        tables = install_table_editors(panel)
        from ui.quick_workspace_layouts import configure_workspace_layout
        configure_workspace_layout(panel, page_id)
        self._relocate_output_actions(panel, page_id)
        forms = install_parameter_forms(panel, page_id)
        forms["references"] = references
        panel.setProperty("quickEditorsInstalled", True)
        self._apply_surface_theme(panel)
        if page_id == "dual_gate" and callable(getattr(panel, "_fit_workflow_layout", None)):
            panel._fit_workflow_layout()
        return panel, tables, forms

    def _release_page_editors(self, page_id):
        entry = self._page_editors.pop(page_id, None)
        self._shutdown_editor_entry(entry)

    @staticmethod
    def _shutdown_editor_entry(entry):
        if entry is not None:
            _panel, tables, forms = entry
            for table in tables:
                table.shutdown()
            for form in forms["forms"]:
                form.shutdown()
            if forms.get("references") is not None:
                forms["references"].shutdown()

    @staticmethod
    def _shutdown_staged_surfaces(panel):
        # Installers may fail before publishing their complete manifests.
        # Release partially built Quick adapters as well as completed ones.
        for widget in panel.findChildren(QWidget):
            if type(widget).__module__.startswith("ui.quick_"):
                shutdown = getattr(widget, "shutdown", None)
                if callable(shutdown):
                    try:
                        shutdown()
                    except Exception:
                        # A constructor may have failed before its timers or
                        # adapters existed. The staging owner deletes it next.
                        pass

    def prepare_page_refresh(self, page_id, old, new, view_state):
        """Validate new Quick surfaces before old subscriptions/UI are touched."""
        from ui.quick_reload_state import restore_quick_view_state
        command = None
        try:
            if page_id == "spectrum":
                command = install_spectrum_controls(new)
            elif page_id == "dual_gate":
                command = install_workflow_controls(new, "Dual Gate", {
                    "apply": new._apply_btn, "run": new._run_btn, "stop": new._stop_btn})
            entry = self._build_page_editors(new, page_id)
            restore_quick_view_state(new, view_state)
            for surface in new.findChildren(QQuickWidget):
                if surface.status() in (QQuickWidget.Status.Error, QQuickWidget.Status.Loading):
                    errors = "\n".join(error.toString() for error in surface.errors())
                    raise RuntimeError(errors or "Quick component has not finished loading")
            return dict(entry=entry, command=command, view_state=view_state,
                        old_entry=self._page_editors.get(page_id), old_command=self._commands.get(page_id))
        except Exception:
            self._shutdown_staged_surfaces(new)
            raise

    def _rebind_calibration_sources(self, page_id, old, new):
        if page_id == "spectrum":
            calibration = getattr(self.window, "_calibration", None)
            if calibration is not None:
                from ui.quick_parameter_forms import rebind_parameter_sources
                rebind_parameter_sources(calibration, {
                    old._use_winspec_nm: new._use_winspec_nm,
                    old._reverse_winspec_pixels: new._reverse_winspec_pixels,
                })

    def commit_page_refresh(self, page_id, old, new, prepared):
        from ui.quick_reload_state import restore_quick_view_state
        # Retain the old editors until the encompassing page transaction
        # succeeds; a command/theme/layout failure can still roll back.
        self._page_editors[page_id] = prepared["entry"]
        command = prepared["command"]
        if command is not None:
            self._commands[page_id] = command
            self._mount_command(command)
            self._bind_admission(page_id, command)
        self._rebind_calibration_sources(page_id, old, new)
        self.refresh()
        restore_quick_view_state(new, prepared["view_state"])

    def rollback_page_refresh(self, page_id, old, new, prepared):
        if prepared["old_entry"] is None:
            self._page_editors.pop(page_id, None)
        else:
            self._page_editors[page_id] = prepared["old_entry"]
        if prepared["old_command"] is None:
            self._commands.pop(page_id, None)
        else:
            self._commands[page_id] = prepared["old_command"]
        command = prepared["command"]
        if command is not None:
            self.command_stack.removeWidget(command)
            command.hide()
            command.setParent(new)
        self._rebind_calibration_sources(page_id, new, old)

    def finish_page_refresh(self, page_id, new, prepared):
        from ui.quick_reload_state import restore_quick_view_state
        self._shutdown_editor_entry(prepared["old_entry"])
        previous = prepared["old_command"]
        if previous is not None:
            self.command_stack.removeWidget(previous)
            previous.shutdown()
            previous.hide()
            previous.deleteLater()
        # Let the newly visible forms settle their measured heights before
        # restoring scroll offsets; do not pump events inside the transaction.
        def restore_after_layout():
            if isValid(new) and self._page_editors.get(page_id, (None,))[0] is new:
                try:
                    restore_quick_view_state(new, prepared["view_state"])
                except Exception as exc:
                    self.window._status.showMessage(
                        f"Interface refreshed, but a view position could not be restored: {exc}", 12000)
        QTimer.singleShot(0, new, lambda: QTimer.singleShot(0, new, restore_after_layout))

    def discard_page_refresh(self, new):
        self._shutdown_staged_surfaces(new)

    def _ensure_sidebar_editors(self):
        panel = getattr(self.window, "_inst_panel", None)
        if panel is not None:
            self._ensure_page_editors(panel, "instruments")
        if self._sidebar_forms is None:
            from ui.quick_sidebar import install_sidebar_forms
            self._sidebar_forms = install_sidebar_forms(self.window)
            self._apply_surface_theme(self.window)

    def _apply_surface_theme(self, panel):
        from ui.quick_scientific_plot import QuickScientificPlot
        from ui.quick_dual_gate_sequence import QuickDualGateSequencePreview
        # Also update hosts whose lazy surface does not exist yet, and the
        # theme copied into a subsequently opened full sequence window.
        for kind in (QuickScientificPlot, QuickDualGateSequencePreview):
            for host in panel.findChildren(kind):
                host.set_theme(self._theme_mode)
        for surface in panel.findChildren(QQuickWidget):
            root = surface.rootObject()
            if root is not None and root.metaObject().indexOfProperty("themeMode") >= 0:
                root.setProperty("themeMode", self._theme_mode)

    @Slot(str)
    def setTheme(self, mode):
        if mode not in {"system", "light", "dark"} or self._closed:
            return
        self._theme_mode = mode
        from ui.quick_style import apply_quick_style
        from PySide6.QtWidgets import QApplication
        apply_quick_style(QApplication.instance(), mode)
        self._apply_surface_theme(self.window)
        self.changed.emit()

    @Property(str, notify=changed)
    def themeMode(self):
        return self._theme_mode

    @Property("QVariantList", notify=pagesChanged)
    def pages(self):
        return self._pages

    @Property(str, notify=changed)
    def sampleId(self):
        return self.runtime.sampleId

    @Property(str, notify=sampleEditorChanged)
    def sampleDraft(self):
        selector = getattr(self.window, "_sample_selector", None)
        return selector.currentText() if selector is not None else self.runtime.sampleId

    @Property(bool, notify=sampleEditorChanged)
    def sampleEditable(self):
        selector = getattr(self.window, "_sample_selector", None)
        return bool(selector is not None and selector.isEditable() and selector.isEnabled()
                    and not selector.lineEdit().isReadOnly())

    def _sync_sample_editor(self, *_args):
        if self._closed:
            return
        binder = self.window._sample_id_binder
        if binder is not self._sample_source_binder:
            self._disconnect_sample_binder()
            self._sample_source_binder = binder
            if hasattr(binder, "valueChanged"):
                binder.valueChanged.connect(self._sample_committed)
        selector = getattr(self.window, "_sample_selector", None)
        edit = selector.lineEdit() if selector is not None else None
        if edit is not self._sample_source_edit:
            if self._sample_source_edit is not None and isValid(self._sample_source_edit):
                self._sample_source_edit.textChanged.disconnect(self._sync_sample_editor)
            self._sample_source_edit = edit
            if edit is not None:
                # The selector blocks its own signals while restoring a sample;
                # its line edit still notifies both presentations of that reset.
                edit.textChanged.connect(self._sync_sample_editor)
        state = self.sampleDraft, self.sampleEditable
        if state != self._sample_editor_state:
            self._sample_editor_state = state
            self.sampleEditorChanged.emit()

    def _disconnect_sample_binder(self):
        binder = self._sample_source_binder
        if isinstance(binder, QObject) and isValid(binder) and hasattr(binder, "valueChanged"):
            binder.valueChanged.disconnect(self._sample_committed)
        self._sample_source_binder = None

    def _sample_committed(self, value):
        if self._closed:
            return
        selector = getattr(self.window, "_sample_selector", None)
        if selector is not None and selector.currentText() != value:
            selector.setEditText(value)
        # Observe commits immediately: a delayed poll must not overwrite a new
        # draft typed just after a sidebar or workflow sample switch.
        self.runtime.sampleId = value
        self._sync_sample_editor()

    @Property(str, notify=changed)
    def statusText(self):
        return self.window._status.currentMessage()

    @Property(str, notify=changed)
    def activityText(self):
        pending = [f"{page.title}: {page.phase}" for page in self.runtime.workflowStates if page.pending]
        return " · ".join(pending) if pending else "Ready"

    def _schedule_navigation_height(self):
        if not self._closed:
            self._navigation_height_timer.start(0)

    def _resize_navigation(self):
        root = self.navigation.rootObject()
        if root is not None:
            # Changing QWidget height from inside QML's synchronous width /
            # implicit-height callback can leave the RHI content viewport at
            # its previous height. Let Qt resize all internal surfaces after
            # that layout pass, rather than resizing quickWindow manually.
            self.navigation.setFixedHeight(max(72, int(root.implicitHeight())))

    def _install_commands(self):
        window = self.window
        self._commands["spectrum"] = install_spectrum_controls(window._spectrum)
        specs = (
            ("dual_gate", window._presets, "Dual Gate", {"apply": "_apply_btn", "run": "_run_btn", "stop": "_stop_btn"}),
            ("mega_sweep", window._mega, "2D Sweep", {"run": "_run_btn", "stop": "_stop_btn"}),
            ("power_sweep", window._power_sweep, "Motion Sweep", {"run": "_run_btn", "stop": "_stop_btn"}),
            ("mcd", window._mcd, "MCD 1000", {"run": "_run_btn", "stop": "_stop_btn", "pause": "_pause_btn"}),
            ("mcd2100", window._mcd2100, "MCD 2100", {"run": "start_btn", "prepare": "prepare_magnet_btn", "stop": "stop_btn"}),
            ("bfp", window._bfp, "BFP acquisition", {"acquire": "_acquire_btn"}),
        )
        for page_id, panel, title, mapping in specs:
            status = (getattr(panel, "_mode_notice", None) if page_id == "mcd"
                      else getattr(panel, "status", None) if page_id == "mcd2100" else None)
            self._commands[page_id] = install_workflow_controls(
                panel, title, {key: getattr(panel, attr) for key, attr in mapping.items()}, status=status,
                legacy_footer=getattr(panel, "_run_footer", None) if page_id in {"mcd", "mcd2100"} else None)

    @Slot(str)
    def selectPage(self, page_id):
        if self._closed:
            return
        for panel, key in self.window._tab_ids.items():
            index = self.window._tabs.indexOf(panel)
            if key == page_id and self.window._tabs.isTabEnabled(index) and self.window._tabs.isTabVisible(index):
                self._show_workspace()
                self._ensure_page_editors(panel, page_id)
                self.window._tabs.setCurrentIndex(index)
                return

    @Slot()
    def toggleSidebar(self):
        if not self._closed:
            self._ensure_sidebar_editors()
            if self.window.width() < 1200 and self._content_splitter is not None:
                self._instrument_focus = not self._instrument_focus
                self._content_splitter.widget(0).setVisible(self._instrument_focus)
                self.window._tabs.setVisible(not self._instrument_focus)
                action = getattr(self.window, "_sidebar_toggle_action", None)
                if action is not None:
                    action.setText("Hide sidebar" if self._instrument_focus else "Show sidebar")
            else:
                self.window._toggle_sidebar()

    @Slot()
    def saveSample(self):
        if self._closed:
            return
        if self._sidebar_forms is not None and hasattr(self._sidebar_forms, "sidebar"):
            from ui.quick_editing import commit_quick_edits
            if not commit_quick_edits(self._sidebar_forms.sidebar):
                return
        self.window._save_current_sample()
        self.refresh()

    @Slot(str)
    def editSampleId(self, value):
        if self._closed or not self.sampleEditable:
            return
        # Share the sidebar's draft without changing the active sample/profile.
        selector = self.window._sample_selector
        selector.setEditText(value)
        selector.lineEdit().textEdited.emit(value)
        self._sync_sample_editor()

    @Slot()
    def resetSampleIdDraft(self):
        if self._closed:
            return
        selector = getattr(self.window, "_sample_selector", None)
        if selector is not None:
            selector.setEditText(self.window._sample_id_binder.value)
        self._sync_sample_editor()

    @Slot(str)
    def commitSampleId(self, value):
        if self._closed or not self.sampleEditable:
            return
        selector = self.window._sample_selector
        # Reuse the selector's sample-switch transaction, including validation,
        # saving the outgoing profile and synchronizing the workflow fields.
        selector.setEditText(value)
        selector.lineEdit().editingFinished.emit()
        self.refresh()

    def refresh(self, *_args):
        if self._closed:
            return
        window = self.window
        if self.runtime.sampleId != window._sample_id_binder.value:
            # Also support preview/runtime stand-ins without a binder signal.
            self._sample_committed(window._sample_id_binder.value)
        self._sync_sample_editor()
        active = window._active_tab_id()
        self._fit_workspace()
        active_panel = window._tabs.currentWidget()
        if active_panel is not None and active:
            self._ensure_page_editors(active_panel, active)
            window._tabs.setMinimumWidth(max(0, active_panel.minimumSizeHint().width()))
            window._tabs.setMinimumHeight(max(0, active_panel.minimumSizeHint().height()))
        command = self._commands.get(active)
        docked = self._dock_page_commands(active_panel, active)
        self.command_stack.setVisible(command is not None and not docked
                                      and (not self._instrument_focus or active in {"mcd", "mcd2100"}))
        self.command_header.setVisible(self.command_stack.isVisibleTo(self.command_header))
        if command is not None and not docked:
            self.command_stack.setCurrentWidget(command)
            self._resize_commands()
        instruments = getattr(window, "_inst_panel", None)
        if instruments is not None and instruments.isVisible():
            self._ensure_sidebar_editors()
        if self._history_context is not None:
            panel, kind, sample = self._history_context
            if (panel is not window._tabs.currentWidget() or sample != self.runtime.sampleId
                    or kind != window._history_type.currentText()):
                self._history_context = None
        if active in {item.pageId for item in self._pages}:
            self.runtime.selectPage(active)
        panels = {page_id: panel for panel, page_id in window._tab_ids.items()}
        for item in self._pages:
            panel = panels[item.pageId]
            index = window._tabs.indexOf(panel)
            item.update(window._tabs.isTabEnabled(index), window._tabs.isTabVisible(index), item.pageId == active)
            if item.pageId in self._dispatching:
                continue
            threads = [getattr(panel, attr, None) for attr in ("_run_thread", "_thread", "thread")]
            thread_busy = any(callable(getattr(thread, "isRunning", None)) and thread.isRunning()
                              for thread in threads)
            finalizing = bool(getattr(panel, "metadata_finalizing", False))
            busy = thread_busy
            if item.pageId == "power_sweep":
                busy = busy or window._power_sweep_running
            elif item.pageId in {"mcd", "mcd2100"}:
                busy = busy or window._active_mcd_panel is panel or bool(getattr(panel, "_interlock_held", False))
            elif item.pageId == "spectrum":
                busy = (panel._pending_acquisition is not None or panel._continuous_mode is not None
                        or panel._abort_btn.isEnabled()
                        or (panel._connected and not panel._action_controls_requested))
                # Retain an observed Spectrum operation through delayed native
                # cancellation cleanup; another workflow's capture is not ours.
                if self.runtime.workflow("spectrum").pending:
                    busy = (busy or bool(getattr(window._lf6, "acquisition_in_progress", False))
                            or bool(getattr(window._lf6, "_settings_pending", False)))
            # A cancelled dialog may close while a native SDK query is still
            # returning. Retain the admitted page's lease until it drains.
            sensor_busy = bool(getattr(panel, '_sensor_preflight_pending', False))
            sensor_busy = sensor_busy or (bool(getattr(window._lf6, 'sensor_preflight_pending', False))
                and self.runtime.workflow(item.pageId).pending)
            busy = busy or sensor_busy
            stopping = self.runtime.workflow(item.pageId).stopping and busy
            phase = "Saving metadata" if finalizing else "Stopping" if stopping else "Running" if busy else "Ready"
            self.runtime.observe(item.pageId, busy=busy, drained=not busy and not finalizing,
                                 metadata_finalizing=finalizing, phase=phase)
        if self._history_widget is not None and self._last_sample != self.runtime.sampleId:
            self._history_widget.setSampleId(self.runtime.sampleId)
            self._last_sample = self.runtime.sampleId

    def _bind_admission(self, page_id, widget):
        widget.adapter.before_dispatch = lambda key: self._before_command(page_id, key)
        widget.adapter.after_dispatch = lambda key: self._after_command(page_id, key)
        widget.adapter.availability = lambda key: self._command_available(page_id, key)

    def _command_available(self, page_id, key):
        starts = key in {"run", "run2d", "acquire", "acquire2d", "prepare"} or (page_id == "spectrum" and key == "apply")
        validates = starts or key == "apply"
        return (not starts or self.runtime.workflow(page_id).canStart) and (not validates or self._parameters_valid(page_id, refresh=False))

    def _parameters_valid(self, page_id, *, refresh=True):
        from ui.quick_parameter_forms import parameters_valid
        entry = self._page_editors.get(page_id)
        if entry is None:
            return True
        panel, tables, _forms = entry
        # Display availability reads the already published field state. Actual
        # dispatch still refreshes and commits all editors before admission.
        return parameters_valid(panel, refresh=refresh) and all(not view.source_table.isEnabled() or not view.model.error for view in tables)

    def _before_command(self, page_id, key):
        self.refresh()
        if key in {"stop", "abort"}:
            self.runtime.request_stop(page_id)
            return True
        if key in {"apply", "run", "run2d", "acquire", "acquire2d", "prepare"} and not self._parameters_valid(page_id):
            self.window._status.showMessage("Review the highlighted parameter or table value before continuing.", 6000)
            return False
        if key in {"apply", "run", "run2d", "acquire", "acquire2d", "prepare"} and page_id in self._page_editors:
            from ui.quick_editing import commit_quick_edits
            if not commit_quick_edits(self._page_editors[page_id][0]):
                return False
        starts = key in {"run", "run2d", "acquire", "acquire2d", "prepare"} or (page_id == "spectrum" and key == "apply")
        if not starts:
            return True
        if not self.runtime.try_start(page_id):
            self.window._status.showMessage(self.runtime.workflow(page_id).blockReason, 6000)
            return False
        self._dispatching.add(page_id)
        return True

    def _after_command(self, page_id, key):
        self._dispatching.discard(page_id)
        self.refresh()

    @Slot(str)
    def _history_kind_changed(self, kind):
        # BFP subpages share a panel object but dispatch settings to different
        # adapters. Invalidate immediately, including a switch away and back
        # between two display refreshes.
        if not self._closed and self._history_context is not None:
            if kind != self._history_context[1]:
                self._history_context = None

    @Slot()
    def showHistory(self):
        if self._closed:
            return
        if self._history_dialog is not None:
            self._history_dialog.show()
            self._history_dialog.raise_()
            self._history_dialog.activateWindow()
            return
        from ui.quick_history import QuickHistoryWidget
        from utils.config import cfg
        window = self.window
        # Bind settings loading to the page that opened this dialog. A page
        # switch, sample switch or active run invalidates that target.
        panel = window._tabs.currentWidget()
        kind = window._history_type.currentText()
        sample = self.runtime.sampleId
        self._history_context = panel, kind, sample

        def supported_keys(request_kind):
            if (request_kind != kind or self._history_context is None
                    or kind != window._history_type.currentText()):
                return None
            method = getattr(panel, window._history_adapters().get(kind, ""), None)
            return method({}, keys_only=True)["supported_keys"] if callable(method) else None

        dialog = QDialog(window)
        dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        dialog.setWindowTitle("Experiment run history")
        dialog.resize(1100, 720)
        widget = QuickHistoryWidget(window._history, cfg.base_out, sample,
                                    supported_keys, dialog)
        if widget.status() == QQuickWidget.Status.Error:
            widget.shutdown()
            dialog.deleteLater()
            raise RuntimeError("\n".join(e.toString() for e in widget.errors()))
        widget.setExperimentType(kind)
        widget.settingsContextLoaded.connect(self._load_history_settings)
        widget.metadataOpenRequested.connect(lambda path: QDesktopServices.openUrl(QUrl.fromLocalFile(path)))
        layout = QVBoxLayout(dialog)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(widget)
        self._history_dialog, self._history_widget = dialog, widget
        self._last_sample = sample
        def finished(_result):
            widget.shutdown()
            self._history_dialog = self._history_widget = None
            self._history_context = None
        dialog.finished.connect(finished)
        dialog.show()

    @Slot(str, dict)
    def _load_history_settings(self, kind, settings):
        if self._closed:
            return
        context = self._history_context
        if context is None:
            self.window._status.showMessage("History settings were not applied: reopen history for the current page and sample.", 8000)
            return
        panel, expected_kind, sample = context
        window = self.window
        if (panel is not window._tabs.currentWidget() or kind != expected_kind
                or expected_kind != window._history_type.currentText()
                or sample != window._sample_id_binder.value or window._acquisition_active()
                or not self.runtime.drained):
            window._status.showMessage("History settings were not applied: the active page, sample or run changed.", 8000)
            return
        method = getattr(panel, window._history_adapters().get(kind, ""), None)
        if not callable(method):
            return
        try:
            report = method(settings)
            window._status.showMessage(f"Loaded compatible settings; skipped: {report.get('skipped', [])}", 8000)
        except Exception as exc:
            window._status.showMessage(f"History settings could not be loaded: {exc}", 8000)

    def rebind_page(self, page_id, old, new):
        # Called after transactional reload has replaced the page successfully.
        if page_id == "spectrum":
            calibration = getattr(self.window, "_calibration", None)
            if calibration is not None:
                from ui.quick_parameter_forms import rebind_parameter_sources
                rebind_parameter_sources(calibration, {
                    old._use_winspec_nm: new._use_winspec_nm,
                    old._reverse_winspec_pixels: new._reverse_winspec_pixels,
                })
        self._release_page_editors(page_id)
        previous = self._commands.pop(page_id, None)
        if previous is not None:
            previous.shutdown()
            parent = previous.parentWidget()
            if parent is not None and parent.layout() is not None:
                parent.layout().removeWidget(previous)
            previous.deleteLater()
        if page_id == "spectrum":
            self._commands[page_id] = install_spectrum_controls(new)
        elif page_id == "dual_gate":
            self._commands[page_id] = install_workflow_controls(new, "Dual Gate", {
                "apply": new._apply_btn, "run": new._run_btn, "stop": new._stop_btn})
        widget = self._commands.get(page_id)
        if widget is not None:
            self._mount_command(widget)
            self._bind_admission(page_id, widget)
        self.refresh()

    def shutdown(self):
        self._closed = True
        if self._sample_source_edit is not None and isValid(self._sample_source_edit):
            self._sample_source_edit.textChanged.disconnect(self._sync_sample_editor)
        self._sample_source_edit = None
        self._disconnect_sample_binder()
        self._history_context = None
        self._timer.stop()
        self._navigation_height_timer.stop()
        self.window.removeEventFilter(self)
        self._dialog_migration.shutdown()
        if self._sidebar_forms is not None:
            self._sidebar_forms.shutdown()
        for page_id in list(self._page_editors):
            self._release_page_editors(page_id)
        if self._history_widget is not None:
            self._history_widget.shutdown()
        for widget in self._commands.values():
            widget.shutdown()
        self.navigation.setSource(QUrl())
