"""Plugin Backup per Claude Code - finestra principale."""
from __future__ import annotations

import ctypes
import datetime
import json
import os
import sys
import traceback
from pathlib import Path

from PySide6.QtCore import QByteArray, QRectF, Qt, QThread, QTimer, Signal
from PySide6.QtGui import QGuiApplication, QKeySequence, QPainter, QPen, QShortcut
from PySide6.QtWidgets import (QApplication, QCheckBox, QFileDialog, QFrame, QHBoxLayout, QLabel, QListWidget,
                               QListWidgetItem, QMainWindow, QMenu, QMessageBox, QPlainTextEdit, QProgressBar,
                               QPushButton, QStackedWidget, QVBoxLayout, QWidget)

import engine
import icons
import theme
from common import (APP_ID, APP_NAME, APP_SHORT, APP_VERSION, APPDATA, CONFIG, ERROR_LOG, LOG_FILE,
                    default_backup_dir, human, open_path, reveal)
from dialogs import AboutDialog, RestoreDialog, SettingsDialog, fmt_date
from theme import P, R_LG, R_SM, R_XL, SP_2, SP_3, SP_4, SP_5, SP_6, qcolor, titlebar
from widgets import Banner, DropOverlay, IconLabel, Toast, a11y_pass, label, sep


# --------------------------------------------------------------------------- #
# Tema
# --------------------------------------------------------------------------- #
def write_ui_assets():
    """Piccole icone SVG usate dal foglio di stile (spunta, freccia dei menu) nei colori del tema."""
    d = os.path.join(APPDATA, "ui")
    os.makedirs(d, exist_ok=True)
    for n, c in {"check.svg": icons.svg("check", "#ffffff", 3),
                 "chev.svg": icons.svg("chevron-down", P.muted, 2.2)}.items():
        with open(os.path.join(d, n), "w", encoding="utf-8") as f:
            f.write(c)
    return d.replace("\\", "/")


def extra_stylesheet():
    return f"""
QLabel#stat {{ font-size: 15pt; font-weight: 600; }}
QLabel#name {{ font-weight: 600; }}
QWidget#row {{ background: {P.surf2}; border: 1px solid {P.border}; border-radius: {R_LG}px; }}
QWidget#row:hover {{ border-color: {P.border_hover}; }}
QWidget#row[sel="true"] {{ background: {P.sel_card}; border: 1px solid {P.accent}; }}
QWidget#row QLabel {{ background: transparent; }}
QWidget#row QLabel#warnbadge {{ color: {P.warn}; background: {P.warn_bg}; border: 1px solid {P.warn_bd};
    border-radius: {R_SM}px; padding: 1px 7px; font-size: {theme.FS_SMALL}pt; }}
QListWidget#backups::item, QListWidget#backups::item:selected, QListWidget#backups::item:hover {{
    background: transparent; border: none; padding: 0; }}
QListWidget#backups {{ outline: 0; }}
QListWidget#plain {{ background: {P.field}; border: 1px solid {P.border}; border-radius: {R_SM}px; padding: 4px; }}
QListWidget#plain::item {{ padding: 4px 6px; }}
QWidget#banner {{ background: {P.info_bg}; border: 1px solid {P.info_bd}; border-radius: {R_LG}px; }}
QWidget#banner[kind="ok"] {{ background: {P.surf2}; border: 1px solid {P.ok}; }}
QWidget#banner[kind="err"] {{ background: {P.danger_bg}; border: 1px solid {P.danger_bd}; }}
QWidget#banner QLabel {{ background: transparent; }}
QPlainTextEdit, QTextBrowser {{ background: {P.field}; border: 1px solid {P.border}; border-radius: {R_SM}px; padding: 6px;
    selection-background-color: {P.accent}; selection-color: {P.on_accent}; }}
QPushButton::menu-indicator {{ image: none; width: 0px; }}
QPushButton#primary {{ min-height: 20px; }}
QMessageBox {{ background: {P.bg}; }}
"""


def apply_theme(app, pref):
    name = theme.apply(pref)
    app.setPalette(theme.app_palette())
    app.setStyleSheet(theme.stylesheet(write_ui_assets()) + extra_stylesheet())
    for w in app.topLevelWidgets():
        if w.isVisible():
            titlebar(w)
    for w in app.allWidgets():
        w.update()
    return name


def load_prefs() -> dict:
    try:
        with open(CONFIG, encoding="utf-8") as f:
            d = json.load(f)
            return d.get("prefs", {}) if isinstance(d, dict) else {}
    except Exception:
        return {}


# --------------------------------------------------------------------------- #
# Lavoro in un thread separato
# --------------------------------------------------------------------------- #
class Worker(QThread):
    progress = Signal(object, str)
    log = Signal(str)
    succeeded = Signal(object)
    failed = Signal(str, str)  # "cancel" | "err", messaggio

    def __init__(self, job, parent=None):
        super().__init__(parent)
        self.job = job
        self.ctx = engine.Ctx(progress=lambda f, t: self.progress.emit(f, t), log=self.log.emit)

    def cancel(self):
        self.ctx.cancel.set()

    def run(self):
        try:
            self.succeeded.emit(self.job(self.ctx))
        except engine.Cancelled:
            self.failed.emit("cancel", "")
        except engine.BackupError as exc:
            self.failed.emit("err", str(exc))
        except PermissionError as exc:
            self.failed.emit("err", "Windows non mi permette di scrivere in questo punto "
                                    f"(file in uso o cartella protetta).\n\n{exc}")
        except Exception as exc:  # noqa: BLE001
            _log_error(traceback.format_exc())
            self.failed.emit("err", f"Errore imprevisto: {exc}\n\nDettagli in {ERROR_LOG}")


def _log_error(text: str):
    try:
        with open(ERROR_LOG, "a", encoding="utf-8") as f:
            f.write(f"\n[{datetime.datetime.now()}]\n{text}")
    except Exception:
        pass


# --------------------------------------------------------------------------- #
# Widget della finestra principale
# --------------------------------------------------------------------------- #
class StatBlock(QWidget):
    def __init__(self, icon, caption):
        super().__init__()
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(SP_3)
        row.addWidget(IconLabel(icon, "accent_text", 24, 1.8), 0, Qt.AlignTop)
        col = QVBoxLayout()
        col.setSpacing(0)
        self.value = QLabel("–")
        self.value.setObjectName("stat")
        self.caption = label(caption, "small")
        col.addWidget(self.value)
        col.addWidget(self.caption)
        row.addLayout(col)

    def set(self, value, caption=None):
        self.value.setText(value)
        if caption is not None:
            self.caption.setText(caption)
        self.setAccessibleName(f"{self.value.text()} {self.caption.text()}")


class EmptyState(QWidget):
    def __init__(self, on_create, on_restore):
        super().__init__()
        lay = QVBoxLayout(self)
        lay.setAlignment(Qt.AlignCenter)
        lay.setSpacing(SP_3)
        lay.addStretch()
        lay.addWidget(IconLabel("archive", "drop_icon", 44, 1.6), 0, Qt.AlignCenter)
        t = label("Nessun backup ancora", "h2")
        t.setAlignment(Qt.AlignCenter)
        lay.addWidget(t)
        st = label("1.  Crea un backup su questo computer\n"
                   "2.  Copia il file .zip sul nuovo computer\n"
                   "3.  Aprilo con questo programma, o trascinalo nella finestra", "muted")
        st.setAlignment(Qt.AlignLeft)
        lay.addWidget(st, 0, Qt.AlignCenter)
        row = QHBoxLayout()
        row.setAlignment(Qt.AlignCenter)
        b1 = QPushButton(icons.icon("archive", "on_accent"), "  Crea il primo backup")
        b1.setObjectName("primary")
        b1.clicked.connect(on_create)
        b2 = QPushButton(icons.icon("download"), "  Ripristina da file…")
        b2.clicked.connect(on_restore)
        for b in (b1, b2):
            b.setCursor(Qt.PointingHandCursor)
            row.addWidget(b)
        lay.addSpacing(SP_2)
        lay.addLayout(row)
        lay.addStretch()
        self.create_btn = b1

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = QRectF(self.rect()).adjusted(2, 2, -2, -2)
        pen = QPen(qcolor(P.drop_border), 1.6, Qt.DashLine)
        pen.setDashPattern([6, 5])
        p.setPen(pen)
        p.setBrush(qcolor(P.drop_fill))
        p.drawRoundedRect(r, R_XL, R_XL)


class BackupRow(QWidget):
    def __init__(self, path: Path, manifest, handlers):
        super().__init__()
        self.path = path
        self.valid = manifest is not None
        self.setObjectName("row")
        self.setAttribute(Qt.WA_StyledBackground, True)
        row = QHBoxLayout(self)
        row.setContentsMargins(SP_4, SP_3, SP_3, SP_3)
        row.setSpacing(SP_3)
        row.addWidget(IconLabel("archive" if self.valid else "alert", "accent_text" if self.valid else "warn", 24, 1.8),
                      0, Qt.AlignVCenter)
        col = QVBoxLayout()
        col.setSpacing(2)
        self.title = label(path.name, "name")
        self.title.setTextFormat(Qt.PlainText)
        col.addWidget(self.title)
        try:
            size = human(path.stat().st_size)
            when = datetime.datetime.fromtimestamp(path.stat().st_mtime).strftime("%d/%m/%Y %H:%M")
        except OSError:
            size, when = "?", "?"
        if manifest:
            parts = [fmt_date(manifest.get("created"))]
            cats = manifest.get("_categories", [])
            if "plugins" in cats:
                parts.append(f"{len(manifest.get('plugins', []))} plugin")
            if "skills" in cats:
                parts.append(f"{len(manifest.get('skills', []))} skill")
            if "agents" in cats:
                parts.append(f"{(manifest.get('counts') or {}).get('agents', 0)} agenti")
            if "mcp" in cats:
                parts.append(f"{len(manifest.get('mcp_servers', []))} MCP")
            parts.append(size)
            detail = "  ·  ".join(parts)
        else:
            detail = f"{when}  ·  {size}  ·  non riconosciuto come backup di Claude Code"
        self.detail = label(detail, "small")
        col.addWidget(self.detail)
        row.addLayout(col, 1)
        if manifest and manifest.get("_legacy_credentials"):
            badge = QLabel("Contiene credenziali")
            badge.setObjectName("warnbadge")
            badge.setToolTip("Creato da una versione precedente. Usa il menu per rimuovere le credenziali dal file.")
            row.addWidget(badge)
        self.restore_btn = QPushButton(icons.icon("restore"), "  Ripristina")
        self.restore_btn.setCursor(Qt.PointingHandCursor)
        self.restore_btn.setEnabled(self.valid)
        if not self.valid:
            self.restore_btn.setToolTip("Questo file non è un backup valido.")
        self.restore_btn.clicked.connect(lambda: handlers["restore"](path))
        row.addWidget(self.restore_btn)
        more = QPushButton(icons.icon("more"), "")
        more.setObjectName("ghost")
        more.setToolTip("Altre azioni")
        more.setAccessibleName(f"Altre azioni per {path.name}")
        more.setCursor(Qt.PointingHandCursor)
        menu = QMenu(more)
        menu.addAction(icons.icon("folder"), "Mostra nel file manager", lambda: handlers["reveal"](path))
        menu.addAction(icons.icon("copy"), "Copia percorso", lambda: handlers["copy"](path))
        if manifest and manifest.get("_legacy_credentials"):
            menu.addSeparator()
            menu.addAction(icons.icon("shield"), "Rimuovi credenziali dal file", lambda: handlers["scrub"](path))
        menu.addSeparator()
        menu.addAction(icons.icon("trash", "err"), "Elimina…", lambda: handlers["delete"](path))
        more.setMenu(menu)
        row.addWidget(more)
        self.more = more

    def set_selected(self, v: bool):
        if self.property("sel") != v:
            self.setProperty("sel", v)
            self.style().unpolish(self)
            self.style().polish(self)

    def set_busy(self, busy: bool):
        self.restore_btn.setEnabled(self.valid and not busy)


# --------------------------------------------------------------------------- #
# Finestra principale
# --------------------------------------------------------------------------- #
class MainWindow(QMainWindow):
    def __init__(self, initial_zip: str | None = None):
        super().__init__()
        self.prefs = load_prefs()
        self.worker: Worker | None = None
        self.status_worker: Worker | None = None
        self.busy = False
        self.rows: dict[str, BackupRow] = {}
        self.setWindowTitle(APP_NAME)
        self.setMinimumSize(780, 600)
        self.resize(900, 700)
        self.setAcceptDrops(True)

        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # --- intestazione
        header = QWidget()
        header.setObjectName("header")
        hl = QHBoxLayout(header)
        hl.setContentsMargins(SP_5, SP_3, SP_4, SP_3)
        hl.setSpacing(SP_3)
        logo = QLabel()
        logo.setPixmap(icons.app_icon_pixmap(96).scaled(34, 34, Qt.KeepAspectRatio, Qt.SmoothTransformation))
        logo.setAccessibleName("Logo")
        hl.addWidget(logo)
        col = QVBoxLayout()
        col.setSpacing(0)
        col.addWidget(label(APP_SHORT, "h1"))
        col.addWidget(label("per Claude Code", "muted"))
        hl.addLayout(col)
        hl.addStretch()
        self.btn_settings = self._ghost("settings", "Impostazioni  (Ctrl+,)", self.open_settings)
        self.btn_about = self._ghost("info", "Informazioni  (F1)", self.open_about)
        hl.addWidget(self.btn_settings)
        hl.addWidget(self.btn_about)
        root.addWidget(header)

        body = QVBoxLayout()
        body.setContentsMargins(SP_6, SP_5, SP_6, SP_3)
        body.setSpacing(SP_4)
        root.addLayout(body, 1)

        self.banner = Banner()
        body.addWidget(self.banner)

        # --- stato di questo computer + azione principale
        card = QFrame()
        card.setObjectName("card")
        cv = QVBoxLayout(card)
        cv.setContentsMargins(SP_5, SP_4, SP_5, SP_4)
        cv.setSpacing(SP_3)
        cl = QHBoxLayout()
        cl.setSpacing(SP_5)
        self.st_plugins = StatBlock("package", "plugin")
        self.st_skills = StatBlock("star", "skill")
        self.st_agents = StatBlock("settings", "agenti")
        self.st_mcp = StatBlock("link", "server MCP")
        for st in (self.st_plugins, self.st_skills, self.st_agents, self.st_mcp):
            cl.addWidget(st)
        cl.addStretch(1)
        self.btn_backup = QPushButton(icons.icon("archive", "on_accent"), "  Crea backup")
        self.btn_backup.setObjectName("primary")
        self.btn_backup.setToolTip("Salva ciò che hai selezionato in un file .zip  (Ctrl+B)")
        self.btn_backup.setCursor(Qt.PointingHandCursor)
        self.btn_backup.clicked.connect(self.do_backup)
        cl.addWidget(self.btn_backup)
        cv.addLayout(cl)
        cv.addWidget(sep())
        inc = QHBoxLayout()
        inc.setSpacing(SP_4)
        inc.addWidget(label("Includi:", "muted"))
        saved = self.prefs.get("include")
        self.chk = {}
        tips = {
            "plugins": "Plugin installati e marketplace, con i loro file (funzionano anche offline).",
            "skills": "Le tue skill in ~/.claude/skills, anche quelle collegate ad altre cartelle. "
                      "Le skill sincronizzate dal tuo account tornano da sole con /login.",
            "agents": "Gli agenti personali in ~/.claude/agents.",
            "commands": "Comandi, regole, stili di output, temi, workflow, CLAUDE.md e scorciatoie.",
            "settings": "settings.json (permessi, modello, tema...). I valori segreti vengono omessi.",
            "mcp": "I server MCP globali. Chiavi API e token vengono omessi: andranno reinseriti.",
        }
        short = {"plugins": "Plugin", "skills": "Skill", "agents": "Agenti", "commands": "Comandi e regole",
                 "settings": "Impostazioni", "mcp": "Server MCP"}
        for cat in engine.CATEGORIES:
            cb = QCheckBox(short[cat])
            cb.setChecked(cat in saved if isinstance(saved, list) else True)
            cb.setToolTip(tips[cat])
            cb.toggled.connect(self._include_changed)
            self.chk[cat] = cb
            inc.addWidget(cb)
        inc.addStretch(1)
        cv.addLayout(inc)
        body.addWidget(card)
        self.card_note = label(self.NOTE_OK, "small", True)
        body.addWidget(self.card_note)

        # --- elenco dei backup
        top = QHBoxLayout()
        top.addWidget(label("Backup salvati", "h2"))
        top.addStretch()
        self.btn_restore_file = QPushButton(icons.icon("download"), "  Ripristina da file…")
        self.btn_restore_file.setToolTip("Scegli un backup .zip da ripristinare  (Ctrl+O)")
        self.btn_restore_file.setCursor(Qt.PointingHandCursor)
        self.btn_restore_file.clicked.connect(self.do_restore_file)
        self.btn_folder = self._ghost("folder", "Apri la cartella dei backup", lambda: self._open_backup_dir())
        self.btn_refresh = self._ghost("refresh", "Aggiorna l'elenco  (F5)", self.refresh_all)
        top.addWidget(self.btn_restore_file)
        top.addWidget(self.btn_folder)
        top.addWidget(self.btn_refresh)
        body.addLayout(top)

        self.stack = QStackedWidget()
        self.empty = EmptyState(self.do_backup, self.do_restore_file)
        self.list = QListWidget()
        self.list.setObjectName("backups")
        self.list.setSpacing(SP_2)
        self.list.setAccessibleName("Elenco dei backup salvati")
        self.list.setVerticalScrollMode(QListWidget.ScrollPerPixel)
        self.list.currentItemChanged.connect(self._sel_changed)
        self.list.itemActivated.connect(lambda it: self.ask_restore(Path(it.data(Qt.UserRole))))
        self.stack.addWidget(self.empty)
        self.stack.addWidget(self.list)
        body.addWidget(self.stack, 1)

        # --- avanzamento (solo durante un'operazione)
        self.panel = QFrame()
        self.panel.setObjectName("card")
        pl = QVBoxLayout(self.panel)
        pl.setContentsMargins(SP_4, SP_3, SP_4, SP_3)
        pl.setSpacing(SP_2)
        prow = QHBoxLayout()
        self.p_label = label("", "name")
        prow.addWidget(self.p_label, 1)
        self.btn_cancel = QPushButton("Annulla")
        self.btn_cancel.setToolTip("Interrompe l'operazione  (Esc)")
        self.btn_cancel.setCursor(Qt.PointingHandCursor)
        self.btn_cancel.clicked.connect(self.cancel_task)
        prow.addWidget(self.btn_cancel)
        pl.addLayout(prow)
        self.bar = QProgressBar()
        self.bar.setTextVisible(False)
        self.bar.setRange(0, 1000)
        pl.addWidget(self.bar)
        self.panel.hide()
        body.addWidget(self.panel)

        # --- registro (a scomparsa)
        logrow = QHBoxLayout()
        self.btn_log = QPushButton(icons.icon("chevron-right", "muted"), "  REGISTRO ATTIVITÀ")
        self.btn_log.setObjectName("section")
        self.btn_log.setCursor(Qt.PointingHandCursor)
        self.btn_log.setCheckable(True)
        self.btn_log.toggled.connect(self._toggle_log)
        logrow.addWidget(self.btn_log)
        logrow.addStretch()
        self.btn_copy_log = QPushButton(icons.icon("copy"), "  Copia")
        self.btn_copy_log.setObjectName("mini")
        self.btn_copy_log.setToolTip("Copia il registro negli appunti")
        self.btn_copy_log.clicked.connect(self.copy_log)
        self.btn_copy_log.hide()
        logrow.addWidget(self.btn_copy_log)
        body.addLayout(logrow)
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(2000)
        self.log_view.setFixedHeight(120)
        self.log_view.setAccessibleName("Registro attività")
        self.log_view.hide()
        body.addWidget(self.log_view)

        # --- piè di pagina
        footer = QWidget()
        footer.setObjectName("footer")
        fl = QHBoxLayout(footer)
        fl.setContentsMargins(SP_5, SP_2, SP_5, SP_2)
        self.status = label("Pronto.", "small")
        fl.addWidget(self.status, 1)
        self.folder_lbl = label("", "small")
        self.folder_lbl.setTextFormat(Qt.PlainText)
        fl.addWidget(self.folder_lbl)
        root.addWidget(footer)

        self.toast = Toast(central)
        self.overlay = DropOverlay(central)

        for keys, fn in (("Ctrl+B", self.do_backup), ("Ctrl+O", self.do_restore_file), ("F5", self.refresh_all),
                         ("Ctrl+,", self.open_settings), ("F1", self.open_about),
                         ("Esc", self.cancel_task), ("Delete", self.delete_current), ("Ctrl+L", self.btn_log.toggle)):
            QShortcut(QKeySequence(keys), self, activated=fn)

        a11y_pass(self)
        g = self.prefs.get("geometry")
        if g:
            try:
                self.restoreGeometry(QByteArray.fromHex(g.encode()))
            except Exception:
                pass
        self.append_log(f"{APP_NAME} {APP_VERSION}\n")
        self.refresh_all()
        if initial_zip:
            QTimer.singleShot(350, lambda: self.ask_restore(Path(initial_zip)))

    # ------------------------------------------------------------------ util
    def _ghost(self, icon, tip, fn):
        b = QPushButton(icons.icon(icon, "text2"), "")
        b.setObjectName("ghost")
        b.setToolTip(tip)
        b.setCursor(Qt.PointingHandCursor)
        b.clicked.connect(fn)
        return b

    def backup_dir(self) -> str:
        return self.prefs.get("backup_dir") or default_backup_dir()

    def set_pref(self, key, value):
        self.prefs[key] = value
        self.save_prefs()

    def save_prefs(self):
        try:
            with open(CONFIG, "w", encoding="utf-8") as f:
                json.dump({"prefs": self.prefs}, f, indent=2)
        except OSError:
            pass

    def set_theme(self, pref):
        self.set_pref("theme", pref)
        apply_theme(QApplication.instance(), pref)

    def _open_backup_dir(self):
        d = self.backup_dir()
        os.makedirs(d, exist_ok=True)
        open_path(d)

    def append_log(self, text: str):
        self.log_view.appendPlainText(text.rstrip("\n"))
        try:
            with open(LOG_FILE, "a", encoding="utf-8") as f:
                f.write(f"[{datetime.datetime.now():%Y-%m-%d %H:%M:%S}] {text.rstrip()}\n")
        except OSError:
            pass

    def copy_log(self):
        QGuiApplication.clipboard().setText(self.log_view.toPlainText())
        self.toast.show_msg("Registro copiato negli appunti")

    def _toggle_log(self, on):
        self.log_view.setVisible(on)
        self.btn_copy_log.setVisible(on)
        self.btn_log.setIcon(icons.icon("chevron-down" if on else "chevron-right", "muted"))

    # ----------------------------------------------------- elenco e stato
    def refresh_all(self):
        self.refresh_backups()
        self.refresh_status()

    def refresh_status(self):
        if self.status_worker and self.status_worker.isRunning():
            return
        self.st_plugins.set("…")
        w = Worker(lambda ctx: engine.machine_status(), self)
        w.succeeded.connect(self._status_ready)
        w.failed.connect(lambda k, m: self._status_ready(None))
        self.status_worker = w
        w.start()

    NOTE_OK = "Le credenziali di accesso non vengono mai salvate; chiavi API e token sono omessi."

    def _status_ready(self, st):
        if not st or not st["found"]:
            for blk in (self.st_plugins, self.st_skills, self.st_agents, self.st_mcp):
                blk.set("–")
            where = st["plugins_dir"] if st else "?"
            self.card_note.setText(f"Non trovo la cartella di Claude Code ({where}). "
                                   "Su questo computer puoi comunque ripristinare un backup.")
            self.card_note.setObjectName("warn")
            self.btn_backup.setEnabled(False)
            self.btn_backup.setToolTip("Non c'è nulla da salvare su questo computer.")
        else:
            self.st_plugins.set(str(st["plugins"]), f"plugin ({st['marketplaces']} marketplace)")
            self.st_skills.set(str(st["skills"]), "skill personali")
            self.st_agents.set(str(st["agents"]), "agenti")
            self.st_mcp.set(str(st["mcp"]), "server MCP")
            self.card_note.setText(self.NOTE_OK)
            self.card_note.setObjectName("small")
            self.btn_backup.setEnabled(not self.busy and any(cb.isChecked() for cb in self.chk.values()))
            self.btn_backup.setToolTip("Salva ciò che hai selezionato in un file .zip  (Ctrl+B)")
        self.card_note.style().unpolish(self.card_note)
        self.card_note.style().polish(self.card_note)

    def _include_changed(self):
        self.set_pref("include", [c for c, cb in self.chk.items() if cb.isChecked()])
        self.btn_backup.setEnabled(not self.busy and any(cb.isChecked() for cb in self.chk.values()))

    def refresh_backups(self):
        folder = self.backup_dir()
        self.folder_lbl.setText(folder)
        self.folder_lbl.setToolTip(folder)
        keep = self.current_path()
        self.list.clear()
        self.rows.clear()
        handlers = {"restore": self.ask_restore, "reveal": reveal, "copy": self.copy_path,
                    "scrub": self.scrub, "delete": self.delete_backup}
        for p in engine.list_backups(folder):
            row = BackupRow(p, engine.read_manifest(p), handlers)
            row.set_busy(self.busy)
            it = QListWidgetItem()
            it.setData(Qt.UserRole, str(p))
            it.setSizeHint(row.sizeHint())
            self.list.addItem(it)
            self.list.setItemWidget(it, row)
            self.rows[str(p)] = row
        a11y_pass(self.list)
        self.stack.setCurrentWidget(self.list if self.list.count() else self.empty)
        if self.list.count():
            target = 0
            for i in range(self.list.count()):
                if self.list.item(i).data(Qt.UserRole) == keep:
                    target = i
            self.list.setCurrentRow(target)

    def current_path(self):
        it = self.list.currentItem()
        return it.data(Qt.UserRole) if it else None

    def _sel_changed(self, cur, prev):
        for k, r in self.rows.items():
            r.set_selected(bool(cur) and cur.data(Qt.UserRole) == k)

    # -------------------------------------------------------- operazioni
    def start(self, title, job, on_ok, kind="backup"):
        if self.busy:
            return
        self.busy = True
        self.op_kind = kind
        self.banner.hide()
        self._set_busy(True, title)
        self.append_log(f"--- {title} ---")
        w = Worker(job, self)
        w.progress.connect(self._on_progress)
        w.log.connect(self.append_log)
        w.succeeded.connect(lambda res: self._finish(on_ok, res))
        w.failed.connect(self._failed)
        self.worker = w
        w.start()

    def _set_busy(self, busy, title=""):
        self.busy = busy
        self.panel.setVisible(busy)
        self.p_label.setText(title)
        self.bar.setRange(0, 0 if busy else 1000)
        self.btn_cancel.setEnabled(busy)
        for b in (self.btn_backup, self.btn_restore_file):
            b.setEnabled(not busy)
        for r in self.rows.values():
            r.set_busy(busy)
        self.empty.setEnabled(not busy)
        self.status.setText(title if busy else "Pronto.")
        if not busy:
            self.refresh_status()

    def _on_progress(self, frac, text):
        if frac is None:
            self.bar.setRange(0, 0)
        else:
            self.bar.setRange(0, 1000)
            self.bar.setValue(int(max(0.0, min(1.0, frac)) * 1000))
        self.p_label.setText(text)
        self.status.setText(text)

    def cancel_task(self):
        if self.busy and self.worker:
            self.worker.cancel()
            self.btn_cancel.setEnabled(False)
            self.p_label.setText("Annullamento in corso…")

    def _finish(self, on_ok, res):
        self._set_busy(False)
        on_ok(res)

    def _failed(self, kind, msg):
        self._set_busy(False)
        self.refresh_backups()
        if kind == "cancel":
            self.append_log("Operazione annullata.")
            if getattr(self, "op_kind", "backup") == "restore":
                self.banner.show_msg("info", "Ripristino annullato: alcune modifiche potrebbero essere già state applicate. "
                                     "Puoi ripeterlo per completarlo, oppure tornare indietro con la copia di sicurezza "
                                     "in ~/.claude/plugin-backup-prima-del-ripristino.")
            else:
                self.banner.show_msg("info", "Operazione annullata. Non è stato lasciato nessun file a metà.")
            return
        self.append_log("ERRORE: " + msg)
        self.banner.show_msg("err", msg, [("Copia dettagli", lambda: self._copy_error(msg)),
                                          ("Mostra registro", lambda: self.btn_log.setChecked(True))])

    def _copy_error(self, msg):
        QGuiApplication.clipboard().setText(msg + "\n\n" + self.log_view.toPlainText()[-4000:])
        self.toast.show_msg("Dettagli copiati negli appunti")

    # --- backup
    def do_backup(self):
        include = {c for c, cb in self.chk.items() if cb.isChecked()}
        if not include:
            self.toast.show_msg("Seleziona almeno una categoria da includere")
            return
        dest = self.backup_dir()
        self.start("Creazione del backup…", lambda ctx: engine.create_backup(dest, ctx, include), self._backup_done)

    def _backup_done(self, res):
        self.refresh_backups()
        for i in range(self.list.count()):
            if self.list.item(i).data(Qt.UserRole) == str(res.path):
                self.list.setCurrentRow(i)
        txt = f"Backup creato: {res.path.name}  ({human(res.size)}, {res.files} file)."
        m = engine.read_manifest(res.path) or {}
        names = sorted({x["name"] for x in m.get("masked", [])})
        if names:
            txt += ("\nPer sicurezza non sono stati salvati questi valori segreti (da reinserire sul nuovo PC): "
                    + ", ".join(names) + ".")
        if res.skipped:
            txt += (f"\n{res.skipped} file in uso sono stati saltati: chiudi Claude Code e ripeti il backup "
                    "per un risultato completo.")
        self.banner.show_msg("ok", txt, [("Mostra nel file manager", lambda: reveal(str(res.path))),
                                         ("Copia percorso", lambda: self.copy_path(res.path))])
        self.toast.show_msg("Backup creato")

    # --- ripristino
    def do_restore_file(self):
        if self.busy:
            return
        path, _ = QFileDialog.getOpenFileName(self, "Scegli un backup da ripristinare", self.backup_dir(),
                                              "Backup ZIP (*.zip);;Tutti i file (*.*)")
        if path:
            self.ask_restore(Path(path))

    def ask_restore(self, path: Path):
        path = Path(path)
        if self.busy:
            self.toast.show_msg("Attendi la fine dell'operazione in corso")
            return
        if not path.is_file():
            self.banner.show_msg("err", f"Il file non esiste più:\n{path}")
            self.refresh_backups()
            return
        manifest = engine.read_manifest(path)
        if manifest is None:
            self.banner.show_msg("err", f"«{path.name}» non è un backup di Claude Code valido "
                                        "(manca il manifest oppure lo zip è danneggiato).")
            return
        dlg = RestoreDialog(self, path, manifest, engine.find_claude() is not None, self.prefs.get("online", True))
        if dlg.exec() != RestoreDialog.Accepted:
            return
        online = dlg.online.isChecked()
        cats = dlg.selected()
        self.set_pref("online", online)
        self.start("Ripristino in corso…", lambda ctx: engine.restore_backup(path, ctx, online, cats),
                   self._restore_done, kind="restore")

    def _restore_done(self, res):
        lines = [f"Ripristino completato: {res.files} file"
                 + (f" ({res.unchanged} già uguali)" if res.unchanged else "") + "."]
        todo = []
        if res.invalid:
            todo.append(f"{res.invalid} voci del backup avevano un nome non valido e sono state ignorate: "
                        "il file potrebbe essere stato modificato.")
        if res.skipped:
            todo.append(f"{res.skipped} file erano in uso e sono stati saltati: chiudi Claude Code e ripeti il ripristino.")
        if res.online_state in ("not_logged_in", "cli_error", "no_cli"):
            todo.append("Reinstallazione online NON eseguita: " + res.online_message)
        elif res.online_state == "done":
            lines.append(f"Reinstallazione online: {res.online_ok} riusciti"
                         + (f", {res.online_failed} non riusciti (vedi il registro)" if res.online_failed else "") + ".")
            if res.remapped:
                lines.append(f"{len(res.remapped)} plugin sono stati reinstallati da un altro catalogo "
                             "(stesso plugin, nome del catalogo diverso).")
        if res.mcp_secrets:
            todo.append("Server MCP da completare, reinserisci chiavi e token: " + "; ".join(res.mcp_secrets) + ".")
        if "skills" in res.categories:
            todo.append("Le skill sincronizzate dal tuo account (docx, pdf, ecc.) tornano da sole dopo il /login.")
        lines.append("Riavvia Claude Code per vedere le modifiche.")
        failed = bool(res.skipped or res.invalid or res.online_failed or res.online_state in ("not_logged_in", "cli_error", "no_cli"))
        actions = [("Mostra registro", lambda: self.btn_log.setChecked(True))]
        if res.snapshot:
            actions.append(("Copia di sicurezza", lambda: open_path(res.snapshot)))
        self.banner.show_msg("info" if failed else "ok", "\n".join(lines + todo), actions)
        self.toast.show_msg("Ripristino completato")

    # --- azioni sui singoli file
    def copy_path(self, path):
        QGuiApplication.clipboard().setText(str(path))
        self.toast.show_msg("Percorso copiato")

    def scrub(self, path: Path):
        def job(ctx):
            ctx.progress(None, "Rimozione delle credenziali dal file…")
            engine.scrub_zip(path)
            return path
        self.start("Rimozione delle credenziali…", job,
                   lambda p: (self.refresh_backups(), self.banner.show_msg("ok", f"Credenziali rimosse da {p.name}.")))

    def delete_current(self):
        p = self.current_path()
        if p and self.list.hasFocus():
            self.delete_backup(Path(p))

    def delete_backup(self, path: Path):
        if self.busy:
            return
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Warning)
        box.setWindowTitle("Elimina backup")
        box.setText(f"Eliminare definitivamente «{path.name}»?")
        box.setInformativeText("Il file non va nel Cestino e non si può recuperare.")
        yes = box.addButton("Elimina", QMessageBox.DestructiveRole)
        no = box.addButton("Annulla", QMessageBox.RejectRole)
        box.setDefaultButton(no)
        titlebar(box)
        box.exec()
        if box.clickedButton() is yes:
            try:
                path.unlink()
                self.append_log(f"Eliminato {path}")
                self.toast.show_msg("Backup eliminato")
            except OSError as exc:
                self.banner.show_msg("err", f"Non riesco a eliminare il file:\n{exc}")
            self.refresh_backups()

    # ----------------------------------------------------------- finestre
    def open_settings(self):
        SettingsDialog(self).exec()

    def open_about(self):
        AboutDialog(self).exec()

    # --------------------------------------------------- eventi di finestra
    def _zip_from(self, mime):
        for u in mime.urls():
            if u.isLocalFile() and u.toLocalFile().lower().endswith(".zip"):
                return u.toLocalFile()
        return None

    def dragEnterEvent(self, e):
        if self._zip_from(e.mimeData()) and not self.busy:
            self.overlay.setGeometry(self.centralWidget().rect())
            self.overlay.raise_()
            self.overlay.show()
            e.acceptProposedAction()

    def dragLeaveEvent(self, e):
        self.overlay.hide()

    def dropEvent(self, e):
        self.overlay.hide()
        z = self._zip_from(e.mimeData())
        if z:
            e.acceptProposedAction()
            QTimer.singleShot(0, lambda: self.ask_restore(Path(z)))

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self.overlay.setGeometry(self.centralWidget().rect())

    def showEvent(self, e):
        titlebar(self)
        super().showEvent(e)

    def closeEvent(self, e):
        if self.busy:
            r = QMessageBox.question(self, "Operazione in corso",
                                     "Un'operazione è ancora in corso. Interromperla e uscire?",
                                     QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            if r != QMessageBox.Yes:
                e.ignore()
                return
            self.cancel_task()
            if self.worker:
                self.worker.wait(20000)
        if self.status_worker and self.status_worker.isRunning():
            self.status_worker.wait(10000)
        self.prefs["geometry"] = bytes(self.saveGeometry().toHex()).decode()
        self.save_prefs()
        super().closeEvent(e)


# --------------------------------------------------------------------------- #
def excepthook(etype, value, tb):
    _log_error("".join(traceback.format_exception(etype, value, tb)))
    try:
        QMessageBox.critical(None, APP_NAME, f"Si è verificato un errore imprevisto:\n\n{value}\n\n"
                                             f"Dettagli in {ERROR_LOG}")
    except Exception:
        pass


def trim_log():
    try:
        if os.path.getsize(LOG_FILE) > 1_000_000:
            with open(LOG_FILE, encoding="utf-8", errors="replace") as f:
                tail = f.read()[-200_000:]
            with open(LOG_FILE, "w", encoding="utf-8") as f:
                f.write(tail)
    except OSError:
        pass


def selftest_restore(zip_path, out_path):
    """`ClaudePluginBackup.exe --selftest-restore <zip> <report.json>`: ripristina senza interfaccia (per collaudi)."""
    lines = []
    report = {"version": APP_VERSION, "frozen": bool(getattr(sys, "frozen", False)),
              "plugins_dir": str(engine.plugins_dir()), "claude_found": engine.find_claude() is not None}
    try:
        res = engine.restore_backup(Path(zip_path), engine.Ctx(log=lines.append))
        report.update(ok=True, files=res.files, skipped=res.skipped, snapshot=res.snapshot)
    except Exception as exc:  # noqa: BLE001
        report.update(ok=False, error=f"{type(exc).__name__}: {exc}")
    report["log"] = lines
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)


def main(argv=None):
    argv = list(sys.argv if argv is None else argv)
    if len(argv) >= 4 and argv[1] == "--selftest-restore":
        selftest_restore(argv[2], argv[3])
        return 0
    sys.excepthook = excepthook
    trim_log()
    if os.name == "nt":
        try:
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("Cesa028." + APP_ID)
        except Exception:
            pass
    QApplication.setHighDpiScaleFactorRoundingPolicy(Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    app = QApplication(argv)
    app.setApplicationName(APP_NAME)
    app.setStyle("Fusion")
    apply_theme(app, load_prefs().get("theme", "system"))
    app.setWindowIcon(icons.app_icon())
    zips = [a for a in argv[1:] if a.lower().endswith(".zip") and os.path.exists(a)]
    w = MainWindow(zips[0] if zips else None)
    w.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
