import unittest
import tempfile
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
        flow.window = Mock()
        flow.window.winfo_id.return_value = 321
        path = Path("원본 캡처.png")
        with patch("modules.flow_window.copy_image_to_clipboard") as copy_image:
            flow._copy_image_path(path)
        copy_image.assert_called_once_with(path, owner_hwnd=321)

    def test_text_block_frame_and_content_both_receive_context_menu(self):
        flow = FlowResultWindow.__new__(FlowResultWindow)
        flow._bind_context_menu = Mock()
        block = Mock()
        label = Mock()
        with (
            patch("modules.flow_window.tk.LabelFrame", return_value=block),
            patch("modules.flow_window.tk.Label", return_value=label),
        ):
            flow._add_text_block(Mock(), "해설")
        bound_widgets = [call.args[0] for call in flow._bind_context_menu.call_args_list]
        self.assertIn(block, bound_widgets)
        self.assertIn(label, bound_widgets)

    def test_image_block_frame_and_preview_both_receive_context_menu(self):
        flow = FlowResultWindow.__new__(FlowResultWindow)
        flow._bind_context_menu = Mock()
        flow._set_preview_placeholder = Mock()
        flow._image_previews = []
        block = Mock()
        preview = Mock()
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "원본.png"
            Image.new("RGB", (40, 20), "red").save(path)
            with (
                patch("modules.flow_window.tk.LabelFrame", return_value=block),
                patch("modules.flow_window.tk.Canvas", return_value=preview),
            ):
                flow._add_image_block(Mock(), path)
        bound_widgets = [call.args[0] for call in flow._bind_context_menu.call_args_list]
        self.assertIn(block, bound_widgets)
        self.assertIn(preview, bound_widgets)

    def test_code_uses_text_category_and_copies_only_source(self):
        flow = FlowResultWindow.__new__(FlowResultWindow)
        flow._bind_context_menu = Mock()
        block = Mock()
        editor = Mock()
        with (
            patch("modules.flow_window.tk.LabelFrame", return_value=block) as label_frame,
            patch("modules.flow_window.ScrolledText", return_value=editor),
        ):
            flow._add_code_block(Mock(), "print('ok')")
        self.assertEqual(label_frame.call_args.kwargs["text"], "텍스트 · 우클릭하여 복사")
        callbacks = [call.args[2] for call in flow._bind_context_menu.call_args_list]
        flow.copy_text_block = Mock()
        callbacks[-1]()
        flow.copy_text_block.assert_called_once_with("print('ok')")

if __name__ == "__main__":
    unittest.main()
