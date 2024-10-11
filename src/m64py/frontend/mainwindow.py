# -*- coding: utf-8 -*-
# Author: Milan Nikolic <gen2brain@gmail.com>
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

import os

from PyQt6.QtGui import QKeySequence, QOpenGLContext, QAction, QActionGroup
from PyQt6.QtWidgets import QMainWindow, QLabel, QFileDialog, QStackedWidget, QSizePolicy, QWidget, QDialog
from PyQt6.QtCore import Qt, QTimer, QFileInfo, QEvent, QMargins, pyqtSignal, pyqtSlot

from m64py.core.defs import *
from m64py.frontend.dialogs import *
from m64py.archive import EXT_FILTER
from m64py.frontend.log import logview
from m64py.frontend.view import View
from m64py.frontend.cheat import Cheat
from m64py.frontend.worker import Worker
from m64py.frontend.rominfo import RomInfo
from m64py.frontend.romlist import ROMList
from m64py.frontend.settings import Settings
from m64py.frontend.glwidget import GLWidget
from m64py.ui.mainwindow_ui import Ui_MainWindow
from m64py.frontend.recentfiles import RecentFiles
from m64py.frontend.keymap import QT2SDL2
from m64py.ui import icons_rc
from m64py.ui import images_rc


class MainWindow(QMainWindow, Ui_MainWindow):
    """Frontend main window"""

    rom_opened = pyqtSignal()
    rom_closed = pyqtSignal()
    file_open = pyqtSignal(str, str)
    file_opening = pyqtSignal(str)
    toggle_fs = pyqtSignal()
    set_caption = pyqtSignal(str)
    state_changed = pyqtSignal(tuple)
    info_dialog = pyqtSignal(str)
    archive_dialog = pyqtSignal(list)
    vidext_set_mode = pyqtSignal(QOpenGLContext)

    def __init__(self, optparse):
        """Constructor"""
        QMainWindow.__init__(self, None)
        self.setupUi(self)
        self.opts, self.args = optparse

        self._initialized = False
        self._initialize_attempt = 0

        logview.setParent(self)
        logview.setWindowFlags(Qt.WindowType.Dialog)

        self.statusbar_label = QLabel()
        self.statusbar_label.setIndent(2)
        self.statusbar_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self.statusbar.addPermanentWidget(self.statusbar_label, 1)
        self.update_status(self.tr("M64Py version %s." % FRONTEND_VERSION))

        self.sizes = {
            SIZE_1X: self.action1X,
            SIZE_2X: self.action2X,
            SIZE_3X: self.action3X}

        self.slots = {}
        self.stack = None
        self.cheats = None
        self.widgets_height = None

        self.glwidget = None

        self.settings = Settings(self)
        self.worker = Worker(self)

        self.vidext = bool(self.settings.get_int_safe("enable_vidext", 1))

        self.create_state_slots()
        self.create_widgets()

        self.installEventFilter(self)
        self.installEventFilter(self.glwidget)

        self.recent_files = RecentFiles(self)
        self.connect_signals()
        self.worker.init()

    def closeEvent(self, event):
        self.worker.quit()

    def showEvent(self, event):
        if not self.widgets_height:
            width, height = self.settings.get_size_safe()
            menubar_height = self.menubar.size().height()
            statusbar_height = self.statusbar.size().height()
            self.widgets_height = menubar_height + statusbar_height
            self.resize(width, height + self.widgets_height)
            self.create_size_actions()
            self.center_widget()

    def eventFilter(self, source, event):
        if event.type() == QEvent.Type.KeyPress:
            if self.worker.state not in (M64EMU_RUNNING, M64EMU_PAUSED):
                return False
            key = event.key()
            modifiers = event.modifiers()
            if modifiers & Qt.KeyboardModifier.AltModifier and (key == Qt.Key.Key_Enter or key == Qt.Key.Key_Return):
                self.toggle_fs.emit()
            else:
                if key in QT2SDL2:
                    self.worker.send_sdl_keydown(QT2SDL2[key])
            return True
        elif event.type() == QEvent.Type.KeyRelease:
            if self.worker.state not in (M64EMU_RUNNING, M64EMU_PAUSED):
                return False
            key = event.key()
            if key in QT2SDL2:
                self.worker.send_sdl_keyup(QT2SDL2[key])
            return True

        return super().eventFilter(source, event)

    def resizeEvent(self, event):
        event.ignore()
        size = event.size()
        if self.widgets_height:
            width, height = size.width(), size.height()
            self.window_size_triggered((width, height))
        else:
            width, height = size.width(), size.height()
            self.resize(width, height)

    def window_size_triggered(self, size):
        width, height = size
        if self.vidext and self.worker.core.get_handle():
            self.worker.core.config.open_section("Video-General")
            self.worker.core.config.set_parameter("ScreenWidth", width)
            self.worker.core.config.set_parameter("ScreenHeight", height - self.widgets_height)

            video_size = ((int(width * self.devicePixelRatio()) << 16) +
                          (int(height * self.devicePixelRatio()) - self.widgets_height))
            if self.worker.state in (M64EMU_RUNNING, M64EMU_PAUSED):
                self.worker.core_state_set(M64CORE_VIDEO_SIZE, video_size)

        self.set_sizes((width, height - self.widgets_height))
        self.settings.qset.setValue("size", (width, height - self.widgets_height))

        self.resize(width, height)

    def set_sizes(self, size):
        """Sets 'Window Size' radio buttons on resize event."""
        width, height = size
        if size in self.sizes.keys():
            self.sizes[(width, height)].setChecked(True)
        else:
            for action in self.sizes.values():
                action.setChecked(False)

    def center_widget(self):
        """Centers widget on desktop."""
        size = self.size()
        desktop = self.screen().geometry()
        width, height = size.width(), size.height()
        dwidth, dheight = desktop.width(), desktop.height()
        cw, ch = (dwidth/2)-(width/2), (dheight/2)-(height/2)
        self.move(int(cw), int(ch))

    def connect_signals(self):
        """Connects signals."""
        self.rom_opened.connect(self.on_rom_opened)
        self.rom_closed.connect(self.on_rom_closed)
        self.file_open.connect(self.on_file_open)
        self.file_opening.connect(self.on_file_opening)
        self.set_caption.connect(self.on_set_caption)
        self.state_changed.connect(self.on_state_changed)
        self.info_dialog.connect(self.on_info_dialog)
        self.archive_dialog.connect(self.on_archive_dialog)
        self.toggle_fs.connect(self.on_toggle_fs)
        self.vidext_set_mode.connect(self.on_vidext_set_mode)

    def create_widgets(self):
        """Creates central widgets."""
        self.stack = QStackedWidget(None)
        palette = self.stack.palette()
        palette.setColor(self.stack.backgroundRole(), Qt.GlobalColor.black)
        self.stack.setPalette(palette)
        self.stack.setAutoFillBackground(True)

        self.glwidget = GLWidget(self)

        self.worker.video.set_widget(self, self.glwidget)

        self.stack.addWidget(View(self))
        self.stack.addWidget(QWidget.createWindowContainer(self.glwidget, self))
        self.stack.setCurrentIndex(0)

        self.setCentralWidget(self.stack)

    def create_state_slots(self):
        """Creates state slot actions."""
        group = QActionGroup(self)
        group.setExclusive(True)
        for slot in range(10):
            self.slots[slot] = QAction(self)
            self.slots[slot].setCheckable(True)
            self.slots[slot].setText("Slot %d" % slot)
            self.slots[slot].setShortcut(QKeySequence(str(slot)))
            self.slots[slot].setActionGroup(group)
            self.menuStateSlot.addAction(self.slots[slot])
        self.slots[0].setChecked(True)
        for slot, action in self.slots.items():
            action.triggered.connect(lambda t, s=slot: self.worker.state_set_slot(s))

    def create_size_actions(self):
        """Creates window size actions."""
        group = QActionGroup(self)
        group.setExclusive(True)
        for num, size in enumerate(
                sorted(self.sizes.keys()), 1):
            width, height = size
            action = self.sizes[size]
            action.setActionGroup(group)
            w, h = width, height+self.widgets_height
            action.setText("%dX" % num)
            action.setToolTip("%sx%s" % (width, height))
            action.triggered.connect(lambda t, wi=w, he=h: self.resize(wi, he))

    def update_status(self, status):
        """Updates label in status bar."""
        self.statusbar_label.setText(status)

    def wait_for_initialize(self):
        """Wait up to 10 seconds for core initialization, checking once a second.
           If not yet initialized, start another QTimer. Else, toggle UI actions."""
        if self.worker.core_state_query(M64CORE_EMU_STATE) == M64EMU_STOPPED:
            self._initialize_attempt += 1
            if self._initialize_attempt < 10:
                QTimer.singleShot(1000, self.wait_for_initialize)
        else:
            self.window_size_triggered((self.width(), self.height()))
            self.worker.toggle_actions()

    def on_vidext_set_mode(self, context):
        context.doneCurrent()
        context.create()
        context.moveToThread(self.worker)
        self._initialized = True

    def on_toggle_fs(self):
        if self.isFullScreen():
            self.menubar.show()
            self.statusbar.show()
            self.glwidget.setCursor(Qt.CursorShape.ArrowCursor)
        else:
            self.menubar.hide()
            self.statusbar.hide()
            self.glwidget.setCursor(Qt.CursorShape.BlankCursor)
        self.setWindowState(self.windowState() ^ Qt.WindowState.WindowFullScreen)

    def on_file_open(self, filepath=None, filename=None):
        """Opens ROM file."""
        if not filepath:
            action = self.sender()
            filepath = action.data()
        if not os.path.isfile(filepath):
            InfoDialog(self, "File %s not found." % filepath).exec()
            return
        self.worker.core_state_query(M64CORE_EMU_STATE)
        if self.worker.state in [M64EMU_RUNNING, M64EMU_PAUSED]:
            self.worker.stop()
        self.worker.set_filepath(filepath, filename)
        self.worker.start()
        self.raise_()

    def on_file_opening(self, filepath):
        """Updates status on file opening."""
        self.update_status("Loading %s..." % (
            os.path.basename(filepath)))

    def on_set_caption(self, title):
        """Sets window title."""
        self.setWindowTitle(title)

    def on_info_dialog(self, info):
        """Shows info dialog."""
        self.settings.show_page(0)
        self.settings.raise_()
        InfoDialog(self.settings, info)

    def on_archive_dialog(self, files):
        """Shows archive dialog."""
        archive = ArchiveDialog(self, files)
        rval = archive.exec()
        if rval == QDialog.DialogCode.Accepted:
            curr_item = archive.listWidget.currentItem()
            fname = curr_item.data(Qt.ItemDataRole.UserRole)
            self.worker.filename = fname

    def on_state_changed(self, states):
        """Toggles actions state."""
        load, pause, action, cheats = states
        self.menuLoad.setEnabled(load)
        self.menuRecent.setEnabled(load)
        self.menuStateSlot.setEnabled(load)
        self.actionLoadState.setEnabled(action)
        self.actionSaveState.setEnabled(action)
        self.actionLoadFrom.setEnabled(action)
        self.actionSaveAs.setEnabled(action)
        self.actionSaveScreenshot.setEnabled(action)
        self.actionShowROMInfo.setEnabled(action)
        self.actionMute.setEnabled(action)
        self.actionStop.setEnabled(action)
        self.actionReset.setEnabled(action)
        self.actionSoftReset.setEnabled(action)
        self.actionLimitFPS.setEnabled(action)
        self.actionSlowDown.setEnabled(action)
        self.actionSpeedUp.setEnabled(action)
        self.actionFullscreen.setEnabled(action)
        self.actionCheats.setEnabled(cheats)
        self.actionPause.setEnabled(pause)
        self.actionPaths.setEnabled(not action)
        self.actionEmulator.setEnabled(not action)
        self.actionGraphics.setEnabled(not action)
        self.actionPlugins.setEnabled(not action)

    def on_rom_opened(self):
        if self.vidext:
            self.stack.setCurrentIndex(1)
        if not self.cheats:
            self.cheats = Cheat(self)
        self.update_status(self.worker.core.rom_settings.goodname.decode())
        """Use QTimer to wait for core initialization before toggling UI actions"""
        QTimer.singleShot(1000, self.wait_for_initialize)

    def on_rom_closed(self):
        if self.vidext and self.isFullScreen():
            self.toggle_fs.emit()
        self.stack.setCurrentIndex(0)
        self.actionMute.setChecked(False)
        self.actionPause.setChecked(False)
        self.actionLimitFPS.setChecked(True)
        self.on_set_caption("M64Py")
        self.update_status("ROM closed.")
        del self.cheats
        self.cheats = None
        self._initialized = False

    @pyqtSlot()
    def on_actionManually_triggered(self):
        """Shows ROM file dialog."""
        dialog = QFileDialog()
        dialog.setFileMode(QFileDialog.FileMode.ExistingFile)
        last_dir = self.settings.qset.value("last_dir")
        file_path, _ = dialog.getOpenFileName(
                self, self.tr("Load ROM Image"), last_dir,
                "Nintendo64 ROM (%s);;All files (*)" % EXT_FILTER)
        if file_path:
            self.file_open.emit(file_path, None)
            last_dir = QFileInfo(file_path).path()
            self.settings.qset.setValue("last_dir", last_dir)

    @pyqtSlot()
    def on_actionFromList_triggered(self):
        """Shows ROM list."""
        ROMList(self)

    @pyqtSlot()
    def on_actionShowROMInfo_triggered(self):
        """Shows ROM information."""
        RomInfo(self)

    @pyqtSlot()
    def on_actionLoadState_triggered(self):
        """Loads state."""
        self.worker.state_load()

    @pyqtSlot()
    def on_actionSaveState_triggered(self):
        """Saves state."""
        self.worker.state_save()

    @pyqtSlot()
    def on_actionLoadFrom_triggered(self):
        """Loads state from file."""
        dialog = QFileDialog()
        dialog.setFileMode(QFileDialog.FileMode.ExistingFile)
        file_path, _ = dialog.getOpenFileName(
            self, self.tr("Load State From File"),
            os.path.join(self.worker.core.config.get_path("UserData"), "save"),
            "M64P/PJ64 Saves (*.st* *.m64p *.zip *.pj);;All files (*)")
        if file_path:
            self.worker.state_load(file_path)

    @pyqtSlot()
    def on_actionSaveAs_triggered(self):
        """Saves state to file."""
        dialog = QFileDialog()
        file_path, file_filter = dialog.getSaveFileName(
            self, self.tr("Save State To File"),
            os.path.join(self.worker.core.config.get_path("UserData"), "save"),
            ";;".join([save_filter for save_filter, save_ext in M64P_SAVES.values()]),
            M64P_SAVES[M64SAV_M64P][0])
        if file_path:
            for save_type, filters in M64P_SAVES.items():
                save_filter, save_ext = filters
                if file_filter == save_filter:
                    if not file_path.endswith(save_ext):
                        file_path = "%s.%s" % (file_path, save_ext)
                    self.worker.state_save(file_path, save_type)


    @pyqtSlot()
    def on_actionSaveScreenshot_triggered(self):
        """Saves screenshot."""
        self.worker.save_screenshot()

    @pyqtSlot()
    def on_actionPause_triggered(self):
        """Toggles pause."""
        self.worker.toggle_pause()

    @pyqtSlot()
    def on_actionMute_triggered(self):
        """Toggles mute."""
        self.worker.toggle_mute()

    @pyqtSlot()
    def on_actionStop_triggered(self):
        """Stops emulator."""
        self.worker.stop()

    @pyqtSlot()
    def on_actionReset_triggered(self):
        """Resets emulator."""
        self.worker.reset()

    @pyqtSlot()
    def on_actionSoftReset_triggered(self):
        """Resets emulator."""
        self.worker.reset(True)

    @pyqtSlot()
    def on_actionLimitFPS_triggered(self):
        """Toggles speed limit."""
        self.worker.toggle_speed_limit()

    @pyqtSlot()
    def on_actionSlowDown_triggered(self):
        """Speeds down emulator."""
        self.worker.speed_down()

    @pyqtSlot()
    def on_actionSpeedUp_triggered(self):
        """Speeds up emulator."""
        self.worker.speed_up()

    @pyqtSlot()
    def on_actionCheats_triggered(self):
        """Shows cheat dialog."""
        if self.cheats:
            self.cheats.show()

    @pyqtSlot()
    def on_actionFullscreen_triggered(self):
        """Toggles fullscreen."""
        self.worker.toggle_fs()

    @pyqtSlot()
    def on_actionPaths_triggered(self):
        """Shows paths settings."""
        self.settings.show_page(0)

    @pyqtSlot()
    def on_actionEmulator_triggered(self):
        """Shows emulator settings."""
        self.settings.show_page(1)

    @pyqtSlot()
    def on_actionGraphics_triggered(self):
        """Shows emulator settings."""
        self.settings.show_page(2)

    @pyqtSlot()
    def on_actionPlugins_triggered(self):
        """Shows plugins settings."""
        self.settings.show_page(3)

    @pyqtSlot()
    def on_actionAbout_triggered(self):
        """Shows about dialog."""
        AboutDialog(self)

    @pyqtSlot()
    def on_actionLicense_triggered(self):
        """Shows license dialog."""
        LicenseDialog(self)

    @pyqtSlot()
    def on_actionLog_triggered(self):
        """Shows log dialog."""
        logview.show()
