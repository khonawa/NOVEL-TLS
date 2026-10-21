import unittest

from novel_translator.epub_builder import split_chapters

BODY = "Some prose sentence here that is long enough.\n\n"
LONG = BODY * 600


def titles(text):
    return [title for title, _ in split_chapters(text)]


class ChapterDetectionTests(unittest.TestCase):
    def test_time_of_day_is_not_a_chapter(self):
        text = "Chapter 3\n\n" + LONG + "3 PM.\n\n" + LONG + "Chapter 5\n\n" + LONG + "Chapter 6\n\n" + BODY
        self.assertEqual(titles(text), ["Chapter 3", "Chapter 5", "Chapter 6"])

    def test_numbered_list_items_are_not_chapters(self):
        text = "Chapter 1\n\n" + BODY + "2. Accessing rooms is difficult.\n\n3. This is the biggest feature.\n\n" + BODY + "Chapter 2\n\n" + BODY
        self.assertEqual(titles(text), ["Chapter 1", "Chapter 2"])

    def test_scene_breaks_are_not_chapters(self):
        text = (
            "Chapter One: The Glass Box\n\n" + LONG + "*** *** ***\n\n" + LONG
            + "=========\n=========\n\n" + "Chapter Two: Cracks\n\n" + LONG + "OK.\n\n"
            + "Chapter Three: Precarious\n\n" + BODY + "==========\n\n" + "Chapter Five: End\n\n" + BODY
        )
        self.assertEqual(titles(text), ["Chapter One: The Glass Box", "Chapter Two: Cracks", "Chapter Three: Precarious", "Chapter Five: End"])

    def test_explicit_numbered_gap_is_still_repaired(self):
        text = "Chapter 1\n\n" + BODY + "Chapter 2\n\n" + BODY + "3 - The Lost One\n\n" + BODY + "Chapter 4\n\n" + BODY
        self.assertEqual(titles(text), ["Chapter 1", "Chapter 2", "3 - The Lost One", "Chapter 4"])

    def test_numeric_only_headings_still_work(self):
        text = "1. Beginning\n\n" + BODY + "2. Middle\n\n" + BODY + "3. End\n\n" + BODY
        self.assertEqual(titles(text), ["1. Beginning", "2. Middle", "3. End"])


if __name__ == "__main__":
    unittest.main()
