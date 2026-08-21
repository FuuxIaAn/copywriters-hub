# -*- coding: utf-8 -*-
"""Focused regression tests for bounded ModelScope TTS recovery."""
import os
import tempfile
import unittest
from unittest.mock import patch

import tts_server


class _Client:
    def __init__(self, action):
        self.action = action

    def predict(self, *args, **kwargs):
        if isinstance(self.action, Exception):
            raise self.action
        return {"value": self.action}


class TtsReliabilityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.output = self.tmp.name
        os.makedirs(os.path.join(self.output, "tts"), exist_ok=True)
        with open(os.path.join(self.output, "tts", "settings.json"), "w", encoding="utf-8") as f:
            f.write('{"token":"test"}')
        self.audio = os.path.join(self.output, "result.wav")
        with open(self.audio, "wb") as f:
            f.write(b"RIFF")
        self.params = {"lang":"ZH", "emo_mode":tts_server.EMO_MODE_SAME, "emo_vec":[],
                       "emo_weight":0.65, "duration_factor":1.0}
        self.original = (tts_server.PREDICT_TIMEOUT, tts_server.GRACE_EXTRA,
                         tts_server.AUTO_RETRY_BUDGET, tts_server.MAX_AUTO_ATTEMPTS)
        tts_server.PREDICT_TIMEOUT = 1
        tts_server.GRACE_EXTRA = 0
        tts_server.AUTO_RETRY_BUDGET = 5
        tts_server.MAX_AUTO_ATTEMPTS = 3

    def tearDown(self):
        (tts_server.PREDICT_TIMEOUT, tts_server.GRACE_EXTRA,
         tts_server.AUTO_RETRY_BUDGET, tts_server.MAX_AUTO_ATTEMPTS) = self.original
        self.tmp.cleanup()

    def test_queue_full_retries_then_succeeds(self):
        clients = iter([_Client(RuntimeError("Queue is full")), _Client(self.audio)])
        with patch.object(tts_server, "_connect_client", side_effect=lambda _: next(clients)), \
             patch.object(tts_server, "_retry_delay", return_value=0):
            src, attempts = tts_server._synthesize_with_retry(self.output, self.audio, "测试", self.params, lambda _: None)
        self.assertEqual(src, self.audio)
        self.assertEqual(attempts, 2)

    def test_ssl_error_recreates_client_then_succeeds(self):
        clients = iter([_Client(OSError("SSL handshake timed out")), _Client(self.audio)])
        with patch.object(tts_server, "_connect_client", side_effect=lambda _: next(clients)), \
             patch.object(tts_server, "_retry_delay", return_value=0):
            _, attempts = tts_server._synthesize_with_retry(self.output, self.audio, "测试", self.params, lambda _: None)
        self.assertEqual(attempts, 2)

    def test_auth_error_is_not_retried(self):
        connect = unittest.mock.Mock(return_value=_Client(RuntimeError("401 Unauthorized")))
        with patch.object(tts_server, "_connect_client", connect):
            with self.assertRaisesRegex(RuntimeError, "鉴权失败"):
                tts_server._synthesize_with_retry(self.output, self.audio, "测试", self.params, lambda _: None)
        self.assertEqual(connect.call_count, 1)

    def test_timeout_has_bounded_attempts(self):
        connect = unittest.mock.Mock(return_value=_Client(TimeoutError("connection timed out")))
        with patch.object(tts_server, "_connect_client", connect), \
             patch.object(tts_server, "_retry_delay", return_value=0):
            with self.assertRaises(TimeoutError):
                tts_server._synthesize_with_retry(self.output, self.audio, "测试", self.params, lambda _: None)
        self.assertEqual(connect.call_count, tts_server.MAX_AUTO_ATTEMPTS)

    def test_inflight_timeout_is_not_classified_as_retryable(self):
        category, retryable = tts_server._error_category(tts_server._InFlightRequestTimeout("still running"))
        self.assertFalse(retryable)


if __name__ == "__main__":
    unittest.main()
