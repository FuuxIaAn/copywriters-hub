# -*- coding: utf-8 -*-
"""不联网的说话人分段回归测试。"""
import pathlib
import sys
import unittest
import extract_server

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from extract_server import _normalize_labeled_segments


class SpeakerSegmentationTests(unittest.TestCase):
    def test_punctuationless_question_answer_is_dialogue(self):
        sentences = ["你现在怎么还要考虑我的女人", "我有等着我钱", "女人不能考虑我", "我不要"]
        self.assertTrue(extract_server._looks_like_dialogue(sentences))
    def setUp(self):
        self.source = [
            "你好我最近失眠。",
            "主要是工作压力太大。",
            "你最近是不是经常凌晨才睡？",
            "是的已经持续两个月了。",
        ]

    def test_combined_llm_segment_never_drops_source_sentences(self):
        raw = [{"speaker": "A", "text": "".join(self.source)}]
        result = _normalize_labeled_segments(raw, self.source)
        self.assertEqual([x["text"] for x in result], self.source)
        self.assertEqual(len(result), len(self.source))

    def test_labels_are_projected_to_each_original_sentence(self):
        raw = [
            {"speaker": "A", "text": self.source[0]},
            {"speaker": "A", "text": self.source[1]},
            {"speaker": "B", "text": self.source[2]},
            {"speaker": "A", "text": self.source[3]},
        ]
        result = _normalize_labeled_segments(raw, self.source)
        self.assertEqual([x["speaker"] for x in result], ["A", "A", "B", "A"])

    def test_merged_return_keeps_multiple_segments(self):
        raw = [
            {"speaker": "A", "text": self.source[0] + self.source[1]},
            {"speaker": "B", "text": self.source[2] + self.source[3]},
        ]
        result = _normalize_labeled_segments(raw, self.source)
        self.assertEqual([x["speaker"] for x in result], ["A", "A", "B", "B"])
        self.assertEqual(len(result), 4)


if __name__ == "__main__":
    unittest.main()
