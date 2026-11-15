"""Input routing regressions; run with QT_QPA_PLATFORM=offscreen python -m unittest discover -s tests."""

import unittest
from types import SimpleNamespace

from PySide6.QtCore import QEvent, QPoint, QPointF, Qt
from PySide6.QtGui import QEventPoint, QInputDevice, QPointingDevice, QTabletEvent
from PySide6.QtWidgets import QApplication
from PySide6.QtTest import QTest

from canvas import PageCanvas


class CanvasInputTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.pen = QPointingDevice(
            "Test pen", 1, QInputDevice.DeviceType.Stylus,
            QPointingDevice.PointerType.Pen, QInputDevice.Capability.Position,
            1, 1,
        )
        cls.eraser = QPointingDevice(
            "Test top eraser", 2, QInputDevice.DeviceType.Stylus,
            QPointingDevice.PointerType.Eraser, QInputDevice.Capability.Position,
            1, 1,
        )

    def setUp(self):
        self.canvas = PageCanvas()
        self.canvas.resize(400, 300)
        self.canvas.show()
        self.app.processEvents()

    def tearDown(self):
        self.canvas.close()
        self.canvas.deleteLater()
        self.app.processEvents()

    def touch(self, kind, x=180, y=180, identifier=1):
        point = SimpleNamespace(
            id=lambda: identifier, position=lambda: QPointF(x, y),
            state=lambda: QEventPoint.State.Updated,
        )
        event = SimpleNamespace(type=lambda: kind, points=lambda: [point], accept=lambda: None)
        self.assertTrue(self.canvas.viewportEvent(event))

    def tablet(self, kind, x=100, y=100, device=None, *, button=None, buttons=None):
        released = kind == QEvent.Type.TabletRelease
        event = QTabletEvent(
            kind, device or self.pen, QPointF(x, y), QPointF(x, y),
            0 if released else 0.5, 0, 0, 0, 0, 0,
            Qt.KeyboardModifier.NoModifier,
            button if button is not None else (Qt.MouseButton.NoButton if kind == QEvent.Type.TabletMove else Qt.MouseButton.LeftButton),
            buttons if buttons is not None else (Qt.MouseButton.NoButton if released else Qt.MouseButton.LeftButton),
        )
        QApplication.sendEvent(self.canvas.viewport(), event)
        self.assertTrue(event.isAccepted())

    def test_touch_pans_without_editing_in_every_tool(self):
        for tool in ("select", "text", "ink", "erase"):
            with self.subTest(tool=tool):
                self.canvas.set_tool(tool)
                self.canvas.verticalScrollBar().setValue(200)
                self.touch(QEvent.Type.TouchBegin)
                self.touch(QEvent.Type.TouchUpdate, y=140)
                self.touch(QEvent.Type.TouchEnd)
                self.assertEqual(self.canvas.verticalScrollBar().value(), 240)
                self.assertEqual(self.canvas.objects(), [])
                self.assertEqual(self.canvas._undo_stack, [])

    def test_pen_writes_in_select_mode_and_undo_removes_whole_stroke(self):
        self.tablet(QEvent.Type.TabletPress)
        self.tablet(QEvent.Type.TabletMove, 120, 130)
        self.tablet(QEvent.Type.TabletRelease, 140, 160)
        self.assertEqual(len(self.canvas.objects()), 1)
        self.assertEqual(self.canvas.objects()[0]["type"], "ink")
        self.assertGreaterEqual(len(self.canvas.objects()[0]["points"]), 3)
        self.canvas.undo()
        self.assertEqual(self.canvas.objects(), [])

    def test_touch_during_pen_stroke_does_not_scroll_or_jump_after_release(self):
        self.canvas.verticalScrollBar().setValue(200)
        self.touch(QEvent.Type.TouchBegin)
        self.tablet(QEvent.Type.TabletPress)
        self.touch(QEvent.Type.TouchUpdate, y=100)
        self.assertEqual(self.canvas.verticalScrollBar().value(), 200)
        self.tablet(QEvent.Type.TabletRelease)
        self.touch(QEvent.Type.TouchUpdate, y=80)
        self.assertEqual(self.canvas.verticalScrollBar().value(), 200)
        self.touch(QEvent.Type.TouchUpdate, y=60)
        self.assertEqual(self.canvas.verticalScrollBar().value(), 220)

    def test_cancel_and_finger_change_reset_pan_anchor(self):
        self.canvas.verticalScrollBar().setValue(200)
        self.touch(QEvent.Type.TouchBegin)
        self.touch(QEvent.Type.TouchUpdate, y=80, identifier=2)
        self.assertEqual(self.canvas.verticalScrollBar().value(), 200)
        self.touch(QEvent.Type.TouchCancel)
        self.touch(QEvent.Type.TouchBegin, y=40)
        self.assertEqual(self.canvas.verticalScrollBar().value(), 200)

    def test_mouse_still_draws_with_pen_tool(self):
        self.canvas.set_tool("ink")
        viewport = self.canvas.viewport()
        QTest.mousePress(viewport, Qt.MouseButton.LeftButton, pos=QPoint(100, 100))
        QTest.mouseMove(viewport, QPoint(130, 140))
        QTest.mouseRelease(viewport, Qt.MouseButton.LeftButton, pos=QPoint(150, 160))
        self.assertEqual(len(self.canvas.objects()), 1)
        self.canvas.undo()
        self.assertEqual(self.canvas.objects(), [])

    def test_pen_eraser_can_be_undone(self):
        self.tablet(QEvent.Type.TabletPress)
        self.tablet(QEvent.Type.TabletRelease, 140, 140)
        original = self.canvas.objects()
        self.canvas.set_tool("erase")
        self.tablet(QEvent.Type.TabletPress, 120, 120)
        self.tablet(QEvent.Type.TabletRelease, 120, 120)
        self.assertEqual(self.canvas.objects(), [])
        self.canvas.undo()
        self.assertEqual(self.canvas.objects(), original)

    def test_top_eraser_preserves_tool_and_pen_tip_resumes_writing(self):
        for tool in ("select", "text", "ink"):
            with self.subTest(tool=tool):
                self.canvas.set_page([])
                self.canvas.set_tool(tool)
                self.tablet(QEvent.Type.TabletPress)
                self.tablet(QEvent.Type.TabletRelease, 140, 140)
                original = self.canvas.objects()
                self.tablet(QEvent.Type.TabletPress, 80, 120, self.eraser)
                self.tablet(QEvent.Type.TabletMove, 120, 120, self.eraser)
                self.tablet(QEvent.Type.TabletRelease, 150, 120, self.eraser)
                self.assertEqual(self.canvas.objects(), [])
                self.assertEqual(self.canvas._tool, tool)
                self.canvas.undo()
                self.assertEqual(self.canvas.objects(), original)
                self.tablet(QEvent.Type.TabletPress, 200, 200)
                self.tablet(QEvent.Type.TabletRelease, 240, 240)
                self.assertEqual(len(self.canvas.objects()), 2)

    def test_side_button_lasso_selects_only_enclosed_ink_without_editing(self):
        for side in (Qt.MouseButton.RightButton, Qt.MouseButton.MiddleButton):
            with self.subTest(side=side):
                self.canvas.set_page([])
                self.canvas.set_tool("ink")
                self.tablet(QEvent.Type.TabletPress, 100, 100)
                self.tablet(QEvent.Type.TabletRelease, 120, 120)
                self.tablet(QEvent.Type.TabletPress, 250, 200)
                self.tablet(QEvent.Type.TabletRelease, 280, 220)
                original = self.canvas.objects()
                undo_count = len(self.canvas._undo_stack)
                self.tablet(QEvent.Type.TabletPress, 80, 80, button=side, buttons=side)
                for x, y in ((150, 80), (150, 150), (80, 150)):
                    self.tablet(QEvent.Type.TabletMove, x, y, buttons=side)
                self.tablet(QEvent.Type.TabletRelease, 80, 80, button=side)
                self.assertEqual(self.canvas.scene().selectedItems(), [self.canvas._items[0]])
                self.assertEqual(self.canvas.objects(), original)
                self.assertEqual(len(self.canvas._undo_stack), undo_count)
                self.assertEqual(self.canvas._tool, "ink")
                self.assertIsNone(self.canvas._lasso)
                self.assertFalse(self.canvas._undo_gesture)
                self.tablet(QEvent.Type.TabletPress, 200, 100)
                self.tablet(QEvent.Type.TabletRelease, 220, 120)
                self.assertEqual(len(self.canvas.objects()), 3)

    def test_lasso_tip_release_finishes_even_with_side_button_held(self):
        side = Qt.MouseButton.RightButton
        self.tablet(QEvent.Type.TabletPress, button=side, buttons=side)
        self.tablet(QEvent.Type.TabletPress, buttons=side | Qt.MouseButton.LeftButton)
        self.tablet(QEvent.Type.TabletMove, 150, 100, buttons=side | Qt.MouseButton.LeftButton)
        self.tablet(QEvent.Type.TabletRelease, 150, 150, buttons=side)
        self.assertIsNone(self.canvas._lasso)
        self.assertEqual(self.canvas.objects(), [])

    def test_escape_cancels_lasso_and_touch_does_not_pan_during_selection(self):
        self.canvas.verticalScrollBar().setValue(200)
        side = Qt.MouseButton.RightButton
        self.tablet(QEvent.Type.TabletPress, button=side, buttons=side)
        self.touch(QEvent.Type.TouchBegin)
        self.touch(QEvent.Type.TouchUpdate, y=100)
        self.assertEqual(self.canvas.verticalScrollBar().value(), 200)
        QTest.keyClick(self.canvas, Qt.Key.Key_Escape)
        self.assertIsNone(self.canvas._lasso)
        self.assertFalse(self.canvas._tablet_active)
        self.assertFalse(self.canvas._undo_gesture)
        self.assertEqual(self.canvas.objects(), [])

    def test_side_button_mid_stroke_preserves_ink_undo(self):
        self.tablet(QEvent.Type.TabletPress)
        self.tablet(QEvent.Type.TabletMove, 120, 120)
        side = Qt.MouseButton.RightButton
        self.tablet(QEvent.Type.TabletPress, 120, 120, button=side,
                    buttons=side | Qt.MouseButton.LeftButton)
        self.tablet(QEvent.Type.TabletRelease, 150, 150, button=side,
                    buttons=Qt.MouseButton.LeftButton)
        self.assertEqual(len(self.canvas.objects()), 1)
        self.canvas.undo()
        self.assertEqual(self.canvas.objects(), [])


if __name__ == "__main__":
    unittest.main()
