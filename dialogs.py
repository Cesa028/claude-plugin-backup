"""Plugin Backup - finestre di dialogo: ripristino, impostazioni, informazioni."""
from __future__ import annotations

import os
import sys
from pathlib import Path

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QDesktopServices, QFont
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QFileDialog, QGridLayout, QHBoxLayout, QLabel,
                               QLineEdit, QListWidget, QListWidgetItem, QPushButton, QTextBrowser, QVBoxLayout)

import engine
import icons
from common import APP_AUTHOR, APP_NAME, APP_URL, APP_VERSION, APPDATA, human, open_path
from theme import SP_1, SP_2, SP_3, SP_4, SP_5, SP_6, THEME_LABELS, THEMES, titlebar
from widgets import a11y_pass, label, sep


def app_file(*parts) -> str:
    """File distribuito con il programma (installato o dai sorgenti)."""
    base = os.path.dirname(sys.executable) if getattr(sys, "frozen", False) else os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base, *parts)


class _Dialog(QDialog):
    def showEvent(self, e):
        titlebar(self)
        super().showEvent(e)


def fmt_date(iso: str) -> str:
    s = str(iso or "").replace("T", " ")
    try:
        d, t = s.split(" ")[:2]
        y, m, dd = d.split("-")
        return f"{dd}/{m}/{y} {t[:5]}"
    except Exception:
        return s or "?"


# --------------------------------------------------------------------------- #
# Conferma del ripristino
# --------------------------------------------------------------------------- #
class RestoreDialog(_Dialog):
    def __init__(self, parent, path: Path, manifest: dict, claude_available: bool, online_default: bool):
        super().__init__(parent)
        self.setWindowTitle("Ripristina backup")
        self.setMinimumWidth(640)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(SP_6, SP_5, SP_6, SP_5)
        lay.setSpacing(SP_3)
        lay.addWidget(label("Ripristinare questo backup?", "h1"))
        lay.addWidget(label(path.name, "muted"))

        plugins = manifest.get("plugins", [])
        cats_in = manifest.get("_categories") or ["plugins"]
        info = f"Creato il {fmt_date(manifest.get('created'))} su {manifest.get('host') or '?'}  \u00b7  {human(path.stat().st_size)}"
        lay.addWidget(label(info, None, True))

        lay.addWidget(label("COSA RIPRISTINARE", "small"))
        counts = {
            "plugins": f"{len(plugins)} plugin, {len(manifest.get('marketplaces', []))} marketplace",
            "skills": f"{len(manifest.get('skills', []))} skill",
            "agents": f"{(manifest.get('counts') or {}).get('agents', 0)} agenti",
            "mcp": f"{len(manifest.get('mcp_servers', []))} server",
        }
        self.cat_boxes = {}
        for cat in cats_in:
            extra = f"  ({counts[cat]})" if cat in counts else ""
            cb = QCheckBox(engine.CATEGORY_LABELS[cat] + extra)
            cb.setChecked(True)
            self.cat_boxes[cat] = cb
            lay.addWidget(cb)

        if "plugins" in self.cat_boxes:  # l'elenco dei plugin sta nel suggerimento (tooltip) della casella
            self.cat_boxes["plugins"].setToolTip("\n".join(
                f"{p.get('id', '?')}  {p.get('version', '')}  ({'attivo' if p.get('enabled', True) else 'disattivato'})"
                for p in plugins) or "Nessun elenco dei plugin nel manifest.")

        lay.addWidget(label("Tutto viene unito a ciò che hai già: i file con lo stesso nome vengono sostituiti "
                            "(la versione precedente di skill, agenti, comandi e dei file di dati resta nella copia di sicurezza).", "info", True))
        if manifest.get("platform") and manifest.get("platform") != sys.platform:
            lay.addWidget(label("Il backup arriva da un altro sistema operativo: i percorsi vengono adattati, "
                                "ma alcuni plugin potrebbero richiedere una reinstallazione.", "warn", True))
        if manifest.get("_legacy_credentials"):
            lay.addWidget(label("Questo file è stato creato con una versione precedente e contiene credenziali "
                                "di accesso. Il ripristino le ignora. Dall'elenco dei backup puoi rimuoverle "
                                "con \"Rimuovi credenziali dal file\".", "warn", True))
        if "mcp" in cats_in and manifest.get("masked"):
            names = ", ".join(sorted({m["name"] for m in manifest["masked"]}))
            lay.addWidget(label("Chiavi API e token non sono nel backup: dopo il ripristino dovrai reinserire "
                                f"{names}.", "warn", True))
        if "skills" in cats_in:
            lay.addWidget(label("Le skill sincronizzate dal tuo account claude.ai (docx, pdf...) non sono file: "
                                "tornano da sole dopo il /login.", "muted", True))
        pv = engine.preview_restore(path)
        risky = []
        if "settings" in cats_in:
            if pv["hooks"]:
                risky.append(f"{pv['hooks']} hook")
            if pv["status_line"]:
                risky.append("una barra di stato (statusLine)")
            if pv["api_key_helper"]:
                risky.append("un comando apiKeyHelper")
        if "mcp" in cats_in and pv["mcp_commands"]:
            risky.append("server MCP: " + "; ".join(f"{n} \u2192 {c[:60]}" for n, c in pv["mcp_commands"][:4]))
        if risky:
            lay.addWidget(label("Questo backup contiene comandi che Claude Code eseguirà sul tuo computer: "
                                + ", ".join(risky) + ". Deseleziona \"Impostazioni\" o \"Server MCP\" se non ti fidi "
                                "di chi ha creato il file.", "warn", True))
        if pv["mismatch"] or pv["invalid_ids"]:
            lay.addWidget(label("L'elenco dei plugin dentro il file non coincide con quello dichiarato oppure contiene "
                                "id non validi: il file potrebbe essere stato modificato. Ripristinalo solo se ti fidi.",
                                "warn", True))
        if "plugins" in cats_in and pv["marketplaces"]:
            lay.addWidget(label("Con la reinstallazione online verranno aggiunti questi marketplace: "
                                + ", ".join(f"{n} ({r})" if r else n for n, r in pv["marketplaces"][:6]), "muted", True))
        lay.addWidget(label("Chiudi Claude Code prima di continuare e riavvialo dopo il ripristino.", "muted", True))

        self.online = QCheckBox("Reinstalla i plugin tramite Claude (serve internet e aver fatto /login)")
        self.online.setChecked(online_default and claude_available and "plugins" in cats_in)
        self.online.setEnabled(claude_available and "plugins" in cats_in)
        if not claude_available:
            self.online.setToolTip("Il comando \"claude\" non è stato trovato su questo computer.")
        lay.addWidget(self.online)
        if "plugins" in self.cat_boxes:
            def _sync(on):
                self.online.setEnabled(on and claude_available)
                if not on:
                    self.online.setChecked(False)
            self.cat_boxes["plugins"].toggled.connect(_sync)

        row = QHBoxLayout()
        row.addStretch()
        cancel = QPushButton("Annulla")
        cancel.clicked.connect(self.reject)
        ok = QPushButton(icons.icon("restore", "on_accent"), "  Ripristina")
        ok.setObjectName("primary")
        ok.setDefault(True)
        ok.clicked.connect(self.accept)
        row.addWidget(cancel)
        row.addWidget(ok)
        lay.addSpacing(SP_1)
        lay.addLayout(row)
        a11y_pass(self)
        ok.setFocus()

    def selected(self):
        return {c for c, cb in self.cat_boxes.items() if cb.isChecked()}


# --------------------------------------------------------------------------- #
# Impostazioni
# --------------------------------------------------------------------------- #
class SettingsDialog(_Dialog):
    def __init__(self, main):
        super().__init__(main)
        self.main = main
        self.setWindowTitle("Impostazioni")
        self.setMinimumWidth(540)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(SP_6, SP_5, SP_6, SP_5)
        lay.setSpacing(SP_3)
        lay.addWidget(label("Impostazioni", "h1"))

        lay.addWidget(label("ASPETTO", "small"))
        row0 = QHBoxLayout()
        row0.addWidget(QLabel("Tema dell'interfaccia"))
        row0.addStretch()
        self.theme_combo = QComboBox()
        for key in THEMES:
            self.theme_combo.addItem(icons.icon({"system": "monitor", "dark": "moon", "light": "sun"}[key]),
                                     THEME_LABELS[key], key)
        cur = main.prefs.get("theme", "system")
        self.theme_combo.setCurrentIndex(THEMES.index(cur) if cur in THEMES else 0)
        self.theme_combo.setFixedWidth(200)
        self.theme_combo.setAccessibleName("Tema dell'interfaccia")
        self.theme_combo.activated.connect(lambda _=0: main.set_theme(self.theme_combo.currentData()))
        row0.addWidget(self.theme_combo)
        lay.addLayout(row0)

        lay.addWidget(sep())
        lay.addWidget(label("CARTELLA DEI BACKUP", "small"))
        row1 = QHBoxLayout()
        self.folder = QLineEdit(main.backup_dir())
        self.folder.setReadOnly(True)
        self.folder.setAccessibleName("Cartella dei backup")
        row1.addWidget(self.folder, 1)
        pick = QPushButton(icons.icon("folder"), "  Cambia…")
        pick.clicked.connect(self.pick_folder)
        row1.addWidget(pick)
        lay.addLayout(row1)

        lay.addWidget(sep())
        lay.addWidget(label("RIPRISTINO", "small"))
        self.online = QCheckBox("Reinstalla i plugin tramite Claude durante il ripristino (serve internet e il login)")
        self.online.setChecked(main.prefs.get("online", True))
        self.online.toggled.connect(lambda v: main.set_pref("online", bool(v)))
        lay.addWidget(self.online)

        row = QHBoxLayout()
        data = QPushButton(icons.icon("folder"), "  Cartella dei dati del programma")
        data.clicked.connect(lambda: open_path(APPDATA))
        row.addWidget(data)
        row.addStretch()
        ok = QPushButton("Chiudi")
        ok.setObjectName("primary")
        ok.setDefault(True)
        ok.clicked.connect(self.accept)
        row.addWidget(ok)
        lay.addSpacing(SP_2)
        lay.addLayout(row)
        a11y_pass(self)

    def pick_folder(self):
        d = QFileDialog.getExistingDirectory(self, "Scegli la cartella dei backup", self.main.backup_dir())
        if d:
            d = os.path.normpath(d)
            self.main.set_pref("backup_dir", d)
            self.folder.setText(d)
            self.main.refresh_backups()


# --------------------------------------------------------------------------- #
# Informazioni e testi di licenza
# --------------------------------------------------------------------------- #
class TextDialog(_Dialog):
    def __init__(self, parent, title, path):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(760, 600)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(SP_5, SP_4, SP_5, SP_4)
        lay.setSpacing(SP_3)
        lay.addWidget(label(title, "h1"))
        view = QTextBrowser()
        view.setOpenExternalLinks(True)
        view.setAccessibleName(title)
        try:
            with open(path, encoding="utf-8-sig") as f:
                view.setPlainText(f.read())
        except OSError:
            view.setPlainText(f"File non trovato:\n{path}")
        mono = QFont("Cascadia Mono")
        mono.setStyleHint(QFont.Monospace)
        mono.setPointSizeF(9.5)
        view.setFont(mono)
        lay.addWidget(view, 1)
        row = QHBoxLayout()
        row.addStretch()
        ok = QPushButton("Chiudi")
        ok.setObjectName("primary")
        ok.clicked.connect(self.accept)
        row.addWidget(ok)
        lay.addLayout(row)
        a11y_pass(self)


class AboutDialog(_Dialog):
    def __init__(self, parent):
        super().__init__(parent)
        self.setWindowTitle(f"Informazioni su {APP_NAME}")
        self.setMinimumWidth(480)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(SP_6, SP_6, SP_6, SP_5)
        lay.setSpacing(SP_2)
        logo = QLabel()
        logo.setPixmap(icons.app_icon_pixmap(160).scaled(80, 80, Qt.KeepAspectRatio, Qt.SmoothTransformation))
        logo.setAlignment(Qt.AlignCenter)
        logo.setAccessibleName("Logo del programma")
        lay.addWidget(logo)
        for text, obj in ((APP_NAME, "h1"), (f"Versione {APP_VERSION}", "muted")):
            lb = label(text, obj)
            lb.setAlignment(Qt.AlignCenter)
            lay.addWidget(lb)
        desc = label("Salva tutti i plugin di Claude Code in un unico file e rimettili su un altro computer "
                     "con un clic. Le credenziali non vengono mai incluse.", None, True)
        desc.setAlignment(Qt.AlignCenter)
        lay.addSpacing(SP_2)
        lay.addWidget(desc)
        cp = label(f"© 2026 {APP_AUTHOR}  ·  Licenza MIT", "small")
        cp.setAlignment(Qt.AlignCenter)
        lay.addSpacing(SP_2)
        lay.addWidget(cp)
        lay.addSpacing(SP_3)
        lay.addWidget(sep())
        lay.addSpacing(SP_1)
        grid = QGridLayout()
        grid.setSpacing(SP_2)
        items = (
            ("scale", "Licenza", lambda: TextDialog(self, "Licenza MIT", app_file("LICENSE.txt")).exec()),
            ("file", "Componenti di terze parti",
             lambda: TextDialog(self, "Componenti di terze parti",
                                app_file("licenses", "THIRD-PARTY-NOTICES.txt")).exec()),
            ("link", "Sito del progetto", lambda: QDesktopServices.openUrl(QUrl(APP_URL))),
            ("folder", "Cartella dei dati", lambda: open_path(APPDATA)),
        )
        for i, (ic, text, fn) in enumerate(items):
            b = QPushButton(icons.icon(ic), "  " + text)
            b.clicked.connect(fn)
            b.setCursor(Qt.PointingHandCursor)
            grid.addWidget(b, i // 2, i % 2)
        lay.addLayout(grid)
        lay.addSpacing(SP_2)
        row = QHBoxLayout()
        row.addStretch()
        ok = QPushButton("Chiudi")
        ok.setObjectName("primary")
        ok.setDefault(True)
        ok.clicked.connect(self.accept)
        row.addWidget(ok)
        lay.addLayout(row)
        a11y_pass(self)
