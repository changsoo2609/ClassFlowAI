import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from PIL import Image

from modules.flow_window import FlowResultWindow, _consume_wheel_delta, _preview_size


class FlowWindowContextCopyTests(unittest.TestCase):
    def test_preview_dimensions_preserve_ratio_and_never_upscale(self):
        self.assertEqual(_preview_size(1520, 920), (760, 460))
        self.assertEqual(_preview_size(100, 50), (100, 50))
        self.assertEqual(_preview_size(2000, 1000), (760, 380))

    def test_mousewheel_uses_stable_notches_and_keeps_touchpad_remainder(self):
        self.assertEqual(_consume_wheel_delta(120), (1, 0))
        self.assertEqual(_consume_wheel_delta(-240), (-2, 0))
        self.assertEqual(_consume_wheel_delta(40, 40), (0, 80))
        self.assertEqual(_consume_wheel_delta(40, 80), (1, 0))

    def test_mousewheel_scrolls_canvas_in_fixed_pixel_units(self):
        flow = FlowResultWindow.__new__(FlowResultWindow)
        flow._wheel_remainder = 0
        flow._preview_refresh_job = None
        flow.window = Mock()
        canvas = Mock()
        event = Mock(delta=-120)
        self.assertEqual(flow._on_mousewheel(event, canvas), "break")
        canvas.yview_scroll.assert_called_once_with(2, "units")

    def test_text_block_uses_tk_clipboard(self):
        flow = FlowResultWindow.__new__(FlowResultWindow)
        flow.window = Mock()
        self.assertTrue(flow._copy_text_value("해설 텍스트"))
        flow.window.clipboard_clear.assert_called_once_with()
        flow.window.clipboard_append.assert_called_once_with("해설 텍스트")

    def test_empty_text_is_not_copied(self):
        flow = FlowResultWindow.__new__(FlowResultWindow)
        flow.window = Mock()
        self.assertFalse(flow._copy_text_value("  "))
        flow.window.clipboard_clear.assert_not_called()

    def test_image_block_copies_original_image_file(self):
        flow = FlowResultWindow.__new__(FlowResultWindow)
        flow.internal_image_copy_callback = Mock()
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "원본 캡처.png"
            Image.new("RGB", (24, 12), "purple").save(path)
            flow._copy_image_path(path)
        copied = flow.internal_image_copy_callback.call_args.args[0]
        self.assertEqual(copied.size, (24, 12))

    def test_context_menu_only_has_separate_image_and_text_copy(self):
        flow = FlowResultWindow.__new__(FlowResultWindow)
        flow.window = Mock()
        flow.copy_image_block = Mock()
        flow.copy_bundle_text = Mock()
        widget = Mock()
        menu = Mock()
        context = {"image_paths": [Path("capture.png")], "text": "해설"}
        with patch("modules.flow_window.tk.Menu", return_value=menu):
            flow._bind_bundle_context_menu(widget, context, Path("capture.png"))
            handler = widget.bind.call_args.args[1]
            handler(Mock(x_root=10, y_root=20))
        labels = [call.kwargs.get("label") for call in menu.add_command.call_args_list]
        self.assertEqual(labels, ["원본 이미지만 복사", "텍스트만 복사"])
        menu.add_command.call_args_list[0].kwargs["command"]()
        menu.add_command.call_args_list[1].kwargs["command"]()
        flow.copy_image_block.assert_called_once_with(Path("capture.png"))
        flow.copy_bundle_text.assert_called_once_with(context)


if __name__ == "__main__":
    unittest.main()
