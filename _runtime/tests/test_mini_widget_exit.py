import unittest
from types import SimpleNamespace
from unittest.mock import patch

from app import ClassFlowAIApp, bind_mini_widget_events, confirm_application_exit


class _Listener:
    def __init__(self):
        self.stop_count = 0

    def stop(self):
        self.stop_count += 1


class _Root:
    def __init__(self):
        self.destroy_count = 0

    def destroy(self):
        self.destroy_count += 1


class _Widget:
    def __init__(self):
        self.bindings = {}

    def bind(self, event_name, callback):
        self.bindings[event_name] = callback


class _Menu:
    def __init__(self, _parent, tearoff=0):
        self.items = []
        self.popup_at = None

    def add_command(self, label, command):
        self.items.append((label, command))

    def add_separator(self):
        self.items.append((None, None))

    def tk_popup(self, x, y):
        self.popup_at = (x, y)

    def grab_release(self):
        return None


class MiniWidgetExitTests(unittest.TestCase):
    def test_mini_widget_has_right_click_menu_on_every_surface(self):
        widgets = [_Widget() for _index in range(4)]
        callbacks = [lambda event=None: None for _index in range(5)]
        bind_mini_widget_events(widgets, *callbacks)
        for widget in widgets:
            self.assertIn("<Button-3>", widget.bindings)
            self.assertIn("<ButtonPress-1>", widget.bindings)
            self.assertIn("<Double-Button-1>", widget.bindings)

        app = object.__new__(ClassFlowAIApp)
        app.mini_status_window = object()
        app.paused = False
        app.restore_main_window = lambda event=None: "break"
        app.toggle_pause = lambda: None
        app.request_app_exit = lambda: False
        with patch("app.tk.Menu", _Menu):
            app.show_mini_context_menu(SimpleNamespace(x_root=30, y_root=30))
        labels = [label for label, _command in app.mini_context_menu.items if label]
        self.assertEqual(labels, ["메인 창 열기", "감지 일시정지", "프로그램 종료"])
        self.assertEqual(app.mini_context_menu.popup_at, (30, 30))

    def test_cancel_does_not_close(self):
        calls = []
        result = confirm_application_exit(lambda _title, _message: False, lambda: calls.append("closed"))
        self.assertFalse(result)
        self.assertEqual(calls, [])

    def test_confirm_calls_existing_close_flow(self):
        calls = []
        result = confirm_application_exit(lambda _title, _message: True, lambda: calls.append("closed"))
        self.assertTrue(result)
        self.assertEqual(calls, ["closed"])

    def test_on_close_is_idempotent_and_stops_listeners(self):
        app = object.__new__(ClassFlowAIApp)
        app.closing = False
        app.running = True
        app.global_keyboard_listener = _Listener()
        app.global_mouse_listener = _Listener()
        app.root = _Root()
        app.stop_execution_timer = lambda save_result=False: None

        app.on_close()
        app.on_close()

        self.assertFalse(app.running)
        self.assertEqual(app.global_keyboard_listener.stop_count, 1)
        self.assertEqual(app.global_mouse_listener.stop_count, 1)
        self.assertEqual(app.root.destroy_count, 1)

if __name__ == "__main__":
    unittest.main()
