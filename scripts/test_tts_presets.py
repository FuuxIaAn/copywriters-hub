# -*- coding: utf-8 -*-
import os
import tempfile
import unittest
import tts_server


class PresetTests(unittest.TestCase):
    def test_update_and_duplicate_marking(self):
        with tempfile.TemporaryDirectory() as out:
            base = tts_server._dir(out)
            up = os.path.join(base, "uploads")
            os.makedirs(up, exist_ok=True)
            ref = os.path.join(up, "a.wav")
            with open(ref, "wb") as f:
                f.write(b"same-audio")
            tts_server._write_json(os.path.join(base, "presets.json"), [
                {"id":"a", "name":"郭律师", "ref_audio":"uploads/a.wav"},
                {"id":"b", "name":"另一名称", "ref_audio":"uploads/a.wav"},
            ])
            listed = tts_server.list_presets(out)["presets"]
            self.assertEqual(len(listed), 1)
            self.assertEqual(listed[0]["duplicate_ids"], ["b"])
            r = tts_server.update_preset(out, "a", "郭律师·主音色", "清晰近讲")
            self.assertTrue(r["ok"])
            self.assertEqual(tts_server.list_presets(out)["presets"][0]["name"], "郭律师·主音色")

    def test_duplicate_upload_reuses_existing_preset(self):
        with tempfile.TemporaryDirectory() as out:
            base = tts_server._dir(out)
            up = os.path.join(base, "uploads")
            os.makedirs(up, exist_ok=True)
            ref = os.path.join(up, "a.wav")
            with open(ref, "wb") as f:
                f.write(b"same-audio")
            first = tts_server.add_preset(out, "郭律师", ref)
            second = tts_server.add_preset(out, "郭律师副本", ref)
            self.assertTrue(first["ok"])
            self.assertTrue(second["duplicate"])
            self.assertEqual(second["preset"]["id"], first["preset"]["id"])


if __name__ == "__main__":
    unittest.main()
