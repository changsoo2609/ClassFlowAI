import unittest
from types import SimpleNamespace
from unittest.mock import Mock

import requests

from modules.model_retry import RETRY_STATUS_TEXT, post_with_transient_retry


def response(status_code):
    return SimpleNamespace(status_code=status_code)


class ModelRetryPolicyTests(unittest.TestCase):
    def call(self, post, on_retry=None):
        requests_module = SimpleNamespace(post=post, exceptions=requests.exceptions)
        sleep = Mock()
        result = post_with_transient_retry(
            requests_module,
            "https://example.invalid/model",
            on_retry=on_retry,
            sleep=sleep,
        )
        return result, sleep

    def test_success_is_not_retried(self):
        post = Mock(return_value=response(200))
        result, sleep = self.call(post)
        self.assertEqual(result.status_code, 200)
        post.assert_called_once()
        sleep.assert_not_called()

    def test_only_selected_transient_http_statuses_retry_once(self):
        for status in (429, 500, 502, 503, 504):
            with self.subTest(status=status):
                post = Mock(side_effect=[response(status), response(200)])
                on_retry = Mock()
                result, sleep = self.call(post, on_retry)
                self.assertEqual(result.status_code, 200)
                self.assertEqual(post.call_count, 2)
                sleep.assert_called_once_with(5.0)
                on_retry.assert_called_once_with(RETRY_STATUS_TEXT)

    def test_permanent_http_errors_are_not_retried(self):
        for status in (400, 401, 403, 404):
            with self.subTest(status=status):
                post = Mock(return_value=response(status))
                result, sleep = self.call(post)
                self.assertEqual(result.status_code, status)
                post.assert_called_once()
                sleep.assert_not_called()

    def test_timeout_and_connection_error_retry_once(self):
        for error in (requests.exceptions.Timeout(), requests.exceptions.ConnectionError()):
            with self.subTest(error=type(error).__name__):
                post = Mock(side_effect=[error, response(200)])
                result, sleep = self.call(post)
                self.assertEqual(result.status_code, 200)
                self.assertEqual(post.call_count, 2)
                sleep.assert_called_once_with(5.0)

    def test_second_transient_failure_is_returned_without_third_attempt(self):
        post = Mock(side_effect=[response(503), response(503)])
        result, sleep = self.call(post)
        self.assertEqual(result.status_code, 503)
        self.assertEqual(post.call_count, 2)
        sleep.assert_called_once_with(5.0)


if __name__ == "__main__":
    unittest.main()
