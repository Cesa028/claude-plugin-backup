"""Plugin Backup - widget riutilizzabili."""
from __future__ import annotations

import re

from PySide6.QtCore import QPropertyAnimation, QRectF, QSize, Qt, QTimer
from PySide6.QtGui import QFont, QPainter, QPen
from PySide6.QtWidgets import (QComboBox, QFrame, QGraphicsOpacityEffect, QHBoxLayout, QLabel, QLayout,
                               QLineEdit, QPushButton, QSlider, QSpinBox, QVBoxLayout, QWidget)

import icons
from theme import DUR_BASE, P, R_XL, SP_2, SP_3, qcolor


class IconLabel(QWidget):
    """Icona vettoriale che si ricolora al cambio di tema. Trasparente al mouse."""

    def __init__(self, name, role="text2", size=16, width=2.0, parent=None):
        super().__init__(parent)
        self.setFixedSize(size, size)
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        self._ic = icons.icon(name, role, width=width)

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        self._ic.paint(p, self.rect())


def sep():
    f = QFrame()
    f.setObjectName("sep")
    return f


def label(text, obj=None, wrap=False):
    lb = QLabel(text)
    if obj:
        lb.setObjectName(obj)
    lb.setWordWrap(wrap)
    return lb


class Toast(QLabel):
    def __init__(self, parent):
        super().__init__(parent)
        self.setObjectName("toast")
        self.setTextFormat(Qt.PlainText)
        self.eff = QGraphicsOpacityEffect(self)
        self.setGraphicsEffect(self.eff)
        self.anim = QPropertyAnimation(self.eff, b"opacity", self)
        self.anim.setDuration(DUR_BASE)
        self.timer = QTimer(self, singleShot=True)
        self.timer.timeout.connect(self._fade_out)
        self._fading = False
        self.anim.finished.connect(lambda: self._fading and self.hide())
        self.hide()

    def show_msg(self, text, ms=2800):
        self.setText(text)
        self.adjustSize()
        par = self.parentWidget()
        self.move((par.width() - self.width()) // 2, par.height() - self.height() - 56)
        self.raise_()
        self.show()
        self.anim.stop()
        self._fading = False
        self.anim.setStartValue(self.eff.opacity() if self.isVisible() else 0.0)
        self.anim.setEndValue(1.0)
        self.anim.start()
        self.timer.start(ms)

    def _fade_out(self):
        self.anim.stop()
        self.anim.setStartValue(1.0)
        self.anim.setEndValue(0.0)
        self._fading = True
        self.anim.start()


class DropOverlay(QWidget):
    """Velo che compare mentre si trascina un file sopra la finestra."""

    def __init__(self, parent):
        super().__init__(parent)
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.hide()

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.fillRect(self.rect(), qcolor(P.overlay))
        r = QRectF(self.rect()).adjusted(24, 24, -24, -24)
        pen = QPen(qcolor(P.accent), 2.2, Qt.DashLine)
        pen.setDashPattern([7, 5])
        p.setPen(pen)
        p.setBrush(qcolor(P.overlay_fill))
        p.drawRoundedRect(r, 18, 18)
        icons.icon("download", "accent_text", width=1.6).paint(
            p, self._rect(r.center().x() - 27, r.center().y() - 70, 54, 54))
        f = QFont()
        f.setPointSizeF(15)
        f.setWeight(QFont.DemiBold)
        p.setFont(f)
        p.setPen(qcolor(P.text))
        p.drawText(QRectF(r.left(), r.center().y() - 6, r.width(), 30), Qt.AlignCenter,
                   "Rilascia il backup per ripristinarlo")
        f.setPointSizeF(10)
        f.setWeight(QFont.Normal)
        p.setFont(f)
        p.setPen(qcolor(P.muted))
        p.drawText(QRectF(r.left(), r.center().y() + 26, r.width(), 22), Qt.AlignCenter,
                   "Prima di procedere ti chiederò conferma")

    @staticmethod
    def _rect(x, y, w, h):
        from PySide6.QtCore import QRect
        return QRect(int(x), int(y), int(w), int(h))


class Banner(QWidget):
    """Messaggio persistente sotto l'intestazione (esito di un'operazione), con azioni e pulsante di chiusura."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("banner")
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.row = QHBoxLayout(self)
        self.row.setContentsMargins(SP_3 + 2, SP_2 + 2, SP_2, SP_2 + 2)
        self.row.setSpacing(SP_3)
        self.icon_host = QHBoxLayout()
        self.text = QLabel()
        self.text.setWordWrap(True)
        self.text.setTextFormat(Qt.PlainText)
        self.text.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.actions = QHBoxLayout()
        self.actions.setSpacing(SP_2)
        self.close_btn = QPushButton(icons.icon("x", "text2"), "")
        self.close_btn.setObjectName("ghost")
        self.close_btn.setToolTip("Chiudi il messaggio")
        self.close_btn.setAccessibleName("Chiudi il messaggio")
        self.close_btn.setCursor(Qt.PointingHandCursor)
        self.close_btn.clicked.connect(self.hide)
        self.row.addLayout(self.icon_host)
        self.row.addWidget(self.text, 1)
        self.row.addLayout(self.actions)
        self.row.addWidget(self.close_btn)
        self.hide()

    def show_msg(self, kind: str, text: str, actions=()):
        """kind: 'info' | 'ok' | 'err'. actions: [(testo, callback)]"""
        self.setProperty("kind", kind)
        while self.icon_host.count():
            w = self.icon_host.takeAt(0).widget()
            if w:
                w.deleteLater()
        icon = {"info": "info", "ok": "check", "err": "alert"}[kind]
        self.icon_host.addWidget(IconLabel(icon, {"info": "info_text", "ok": "ok", "err": "err"}[kind], 18, 2.2))
        self.text.setText(text)
        while self.actions.count():
            w = self.actions.takeAt(0).widget()
            if w:
                w.deleteLater()
        for caption, cb in actions:
            b = QPushButton(caption)
            b.setCursor(Qt.PointingHandCursor)
            b.clicked.connect(cb)
            self.actions.addWidget(b)
        self.style().unpolish(self)
        self.style().polish(self)
        self.show()
        QTimer.singleShot(0, self._fit)

    def _fit(self):
        """Un'etichetta con a capo automatico dentro un layout orizzontale non comunica l'altezza giusta:
        la ricalcoliamo in base alla larghezza reale."""
        self.text.setMinimumHeight(0)
        self.layout().activate()
        wd = self.text.width()
        if wd > 0:
            self.text.setMinimumHeight(self.text.heightForWidth(wd))
        self.updateGeometry()

    def resizeEvent(self, e):
        super().resizeEvent(e)
        if self.isVisible():
            QTimer.singleShot(0, self._fit)


def a11y_pass(root):
    """Accessibilità: focus dei pulsanti solo da tastiera, nome ai pulsanti con sola icona, nomi ai campi."""
    for b in root.findChildren(QPushButton):
        b.setFocusPolicy(Qt.TabFocus)
        if not b.text().strip() and not b.accessibleName():
            tip = b.toolTip().split("  (")[0].strip()
            if tip:
                b.setAccessibleName(tip)
    layouts = ([root.layout()] if root.layout() else []) + root.findChildren(QLayout)
    for lay in layouts:
        last = ""
        for i in range(lay.count()):
            w = lay.itemAt(i).widget()
            if isinstance(w, QLabel):
                txt = re.sub(r"<[^>]+>", "", w.text()).strip()
                if txt:
                    last = txt
            elif isinstance(w, (QComboBox, QSpinBox, QSlider, QLineEdit)) and not w.accessibleName():
                name = last or w.toolTip()
                if name:
                    w.setAccessibleName(name)
