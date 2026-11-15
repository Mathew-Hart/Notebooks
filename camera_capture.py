"""Preview a system camera and capture a still image in memory."""

import sys

from PySide6.QtCore import QCameraPermission, Qt, QTimer
from PySide6.QtGui import QImage
from PySide6.QtWidgets import (
    QApplication, QComboBox, QDialog, QDialogButtonBox, QFormLayout, QLabel, QVBoxLayout,
)


class CameraCaptureDialog(QDialog):
    def __init__(self, parent=None):
        # Multimedia is optional in some PySide6 distributions.
        from PySide6.QtMultimedia import QImageCapture, QMediaCaptureSession, QMediaDevices
        from PySide6.QtMultimediaWidgets import QVideoWidget

        super().__init__(parent)
        self.setWindowTitle("Insert Camera Photo")
        self.resize(640, 480)
        self.image = QImage()
        self._opened = False
        self._closed = False
        self._request_id = None
        self._devices = QMediaDevices(self)
        self._inputs = []
        self._camera = None
        self._image_capture = QImageCapture(self)
        self._session = QMediaCaptureSession(self)
        self._preview = QVideoWidget(self)
        self._preview.setMinimumSize(320, 240)

        self._device_picker = QComboBox(self)
        self._device_picker.setEnabled(False)
        self._status = QLabel("Starting camera…", self)
        self._status.setWordWrap(True)
        self._status.setTextFormat(Qt.TextFormat.PlainText)
        self._buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        self._capture_button = self._buttons.addButton("Capture", QDialogButtonBox.ButtonRole.ActionRole)
        self._capture_button.setEnabled(False)

        options = QFormLayout()
        options.addRow("Camera", self._device_picker)
        layout = QVBoxLayout(self)
        layout.addLayout(options)
        layout.addWidget(self._preview, 1)
        layout.addWidget(self._status)
        layout.addWidget(self._buttons)

        self._session.setVideoOutput(self._preview)
        self._session.setImageCapture(self._image_capture)
        self._image_capture.readyForCaptureChanged.connect(self._update_capture_button)
        self._image_capture.imageCaptured.connect(self._image_captured, Qt.ConnectionType.QueuedConnection)
        self._image_capture.errorOccurred.connect(self._capture_error, Qt.ConnectionType.QueuedConnection)
        self._device_picker.currentIndexChanged.connect(self._start_camera)
        self._capture_button.clicked.connect(self._take_picture)
        self._buttons.rejected.connect(self.reject)

        self._capture_timeout = QTimer(self, singleShot=True, interval=10000)
        self._capture_timeout.timeout.connect(
            lambda: self._capture_failed("The camera did not return a photo. Try capturing again.")
        )

    def showEvent(self, event):
        super().showEvent(event)
        if not self._opened:
            self._opened = True
            QTimer.singleShot(0, self, self._request_permission)

    def _request_permission(self):
        if self._closed:
            return
        if sys.platform == "darwin" and not (
            getattr(sys, "frozen", False) or "__compiled__" in globals()
        ):
            self._status.setText("Camera access on macOS requires a packaged app with camera permission.")
            return
        app = QApplication.instance()
        permission = QCameraPermission()
        status = app.checkPermission(permission)
        if status == Qt.PermissionStatus.Undetermined:
            self._status.setText("Waiting for camera permission…")
            app.requestPermission(permission, self, lambda result: self._permission_ready(result.status()))
        else:
            self._permission_ready(status)

    def _permission_ready(self, status):
        if self._closed:
            return
        if status != Qt.PermissionStatus.Granted:
            self._status.setText("Camera access was denied. Allow camera access in your system settings.")
            return
        self._inputs = self._devices.videoInputs()
        if not self._inputs:
            self._status.setText("No camera found. Connect a camera and reopen this dialog.")
            return
        from PySide6.QtMultimedia import QCamera

        default_id = self._devices.defaultVideoInput().id()
        default = next((index for index, device in enumerate(self._inputs) if device.id() == default_id), 0)
        self._camera = QCamera(self._inputs[default], self)
        self._camera.activeChanged.connect(self._update_capture_button)
        self._camera.errorOccurred.connect(self._camera_error)
        self._session.setCamera(self._camera)
        self._device_picker.blockSignals(True)
        self._device_picker.addItems([device.description() for device in self._inputs])
        self._device_picker.setCurrentIndex(default)
        self._device_picker.blockSignals(False)
        self._device_picker.setEnabled(len(self._inputs) > 1)
        self._start_camera(default)

    def _start_camera(self, index):
        if self._closed or not 0 <= index < len(self._inputs):
            return
        self._capture_button.setEnabled(False)
        self._status.setText("Starting camera…")
        self._camera.stop()
        self._camera.setCameraDevice(self._inputs[index])
        self._camera.start()

    def _update_capture_button(self):
        ready = (
            not self._closed and self._request_id is None
            and self._camera is not None
            and self._camera.isActive() and self._image_capture.isReadyForCapture()
        )
        self._capture_button.setEnabled(ready)
        if ready and self._status.text() == "Starting camera…":
            self._status.clear()

    def _take_picture(self):
        if not self._capture_button.isEnabled():
            return
        self._capture_button.setEnabled(False)
        self._device_picker.setEnabled(False)
        self._status.setText("Capturing…")
        self._request_id = self._image_capture.capture()
        if self._request_id < 0:
            self._capture_failed(self._image_capture.errorString() or "The camera could not take a photo.")
        else:
            self._capture_timeout.start()

    def _image_captured(self, request_id, image):
        if self._closed or request_id != self._request_id:
            return
        if image.isNull():
            self._capture_failed("The camera returned an empty photo. Try capturing again.")
            return
        self.image = image.copy()
        self.accept()

    def _capture_error(self, request_id, error, message):
        if request_id == self._request_id:
            self._capture_failed(message or "The camera could not take a photo.")

    def _camera_error(self, error, message):
        self._capture_failed(message or "The camera could not be started.")

    def _capture_failed(self, message):
        if self._closed:
            return
        self._request_id = None
        self._capture_timeout.stop()
        self._status.setText(message)
        self._device_picker.setEnabled(len(self._inputs) > 1)
        self._update_capture_button()

    def done(self, result):
        self._closed = True
        self._capture_timeout.stop()
        if self._camera is not None:
            self._camera.stop()
        self._session.setCamera(None)
        self._session.setImageCapture(None)
        self._session.setVideoOutput(None)
        super().done(result)
