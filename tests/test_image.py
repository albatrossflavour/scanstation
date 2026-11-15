import shutil
import unittest

import numpy as np
from PIL import Image, ImageDraw, ImageFont

from scanstation.image import correct, find_angle, is_blank, page_flags, straighten_sheet, upright

FRONT_BACKING = (180, 195, 198)
BACK_BACKING = (200, 219, 222)


def text_page(w=1200, h=1600):
    """White paper with dark bars standing in for lines of text."""
    a = np.full((h, w, 3), 245, np.uint8)
    for y in range(120, h - 120, 40):
        a[y:y + 12, 100:w - 100] = 30
    return Image.fromarray(a)


def on_backing(page, backing, tilt=0.0, margin=80):
    """Place a page on scanner backing, tilted as a crooked feed would be."""
    tilted = page.rotate(tilt, resample=Image.BICUBIC, expand=True, fillcolor=backing)
    canvas = Image.new("RGB", (tilted.width + 2 * margin, tilted.height + 2 * margin), backing)
    # Paste only the paper, not the rotated-in fill, by masking on the fill colour.
    mask = Image.fromarray((np.abs(np.asarray(tilted, int) - backing).max(2) > 0).astype(np.uint8) * 255)
    canvas.paste(tilted, (margin, margin), mask)
    return canvas


class StraightenTest(unittest.TestCase):
    def test_straighten_recovers_tilt(self):
        page = text_page()
        front = on_backing(page, FRONT_BACKING, tilt=4.0)
        back = on_backing(page.transpose(Image.FLIP_LEFT_RIGHT), BACK_BACKING, tilt=-4.0)
        f, b, _ = straighten_sheet(front, back)
        for out in (f, b):
            self.assertLess(abs(find_angle(out)[0]), 0.3)
            self.assertLess(abs(out.width - page.width), 15)
            self.assertLess(abs(out.height - page.height), 15)

    def test_crop_ignores_card_shadow(self):
        card = text_page(1000, 640)
        scan = on_backing(card, FRONT_BACKING, margin=100)
        a = np.asarray(scan).copy()
        shadow = tuple(v - 25 for v in FRONT_BACKING)
        # A 30 px shadow below and to the right of the card.
        a[100 + 640:100 + 670, 130:100 + 1030] = shadow
        a[130:100 + 670, 100 + 1000:100 + 1030] = shadow
        scan = Image.fromarray(a)
        f, _, _ = straighten_sheet(scan, on_backing(card, BACK_BACKING, margin=100))
        # Card-sized: the whole card plus a few mm, shadow and all, rather than risk cutting into it.
        self.assertGreaterEqual(f.width, 1000)
        self.assertGreaterEqual(f.height, 640)
        self.assertLess(f.width, 1000 + 120)   # the 30 px shadow plus 3 mm either side
        self.assertLess(f.height, 640 + 120)

    def test_crop_drops_trailing_edge_shadow(self):
        page = text_page()
        scan = np.asarray(on_backing(page, FRONT_BACKING, margin=100)).copy()
        # The scanner sees a near-black band just past the paper's trailing edge.
        scan[100 + 1600:100 + 1615, 90:100 + 1210] = (50, 50, 52)
        f, _, _ = straighten_sheet(Image.fromarray(scan), on_backing(page, BACK_BACKING, margin=100))
        self.assertLess(f.height, 1600 + 4)
        bottom = np.asarray(f.convert("L"))[-8:]
        self.assertLess((bottom < 120).mean(), 0.05)

    def test_card_is_never_cropped_into(self):
        # From the real licence: a dark band near the top edge and dark print close to the left
        # edge were both trimmed as shadow. Card-sized items keep everything, plus a little margin.
        card = np.full((640, 1010, 3), 235, np.uint8)       # 85.6 x 54 mm at 300 dpi
        card[30:110, :] = (40, 110, 115)                     # teal band 2.5 mm from the top
        card[200:260, 0:40] = 20                             # print right at the left edge
        card[560:600, 960:1010] = 20                         # and at the bottom right
        front = on_backing(Image.fromarray(card), FRONT_BACKING, margin=120)
        a = np.asarray(front).copy()
        shadow = tuple(v - 25 for v in FRONT_BACKING)
        a[120 + 640:120 + 665, 140:120 + 1035] = shadow    # the card's own shadow
        f, _, _ = straighten_sheet(Image.fromarray(a), on_backing(Image.fromarray(card), BACK_BACKING, margin=120))
        self.assertGreaterEqual(f.width, 1010)
        self.assertGreaterEqual(f.height, 640)
        self.assertLess(f.width, 1010 + 100)                # a few mm of margin, not the whole scan


class CorrectTest(unittest.TestCase):
    def test_correct_keeps_hue(self):
        a = np.full((200, 200, 3), 230, np.uint8)
        a[50:150, 50:150] = (131, 49, 48)
        out = np.asarray(correct(Image.fromarray(a)).convert("RGB"), int)[100, 100]
        self.assertEqual(int(out.argmax()), 0)
        self.assertGreater(out[0] - out[1], 80)

    def test_correct_greyscale_for_colourless_page(self):
        page = text_page()
        self.assertEqual(correct(page).mode, "L")
        a = np.asarray(page).copy()
        a[200:600, 200:500] = (200, 30, 30)  # about 6% of the page
        self.assertEqual(correct(Image.fromarray(a)).mode, "RGB")


class BlankAndFlagsTest(unittest.TestCase):
    def test_blank_page_detected(self):
        rng = np.random.default_rng(1)
        a = np.clip(250 + rng.normal(0, 3, (1600, 1200)), 0, 255).astype(np.uint8)
        a[400, 100:1100] = 235  # faint show-through from the other side
        a[900, 100:1100] = 235
        self.assertTrue(is_blank(Image.fromarray(a)))

    def test_blank_detection_keeps_sparse_forms(self):
        a = np.full((1600, 1200), 250, np.uint8)
        a[300:1100:80, 100:1100] = 60  # a 1 px grid of form boxes over 40% of the page
        a[300:1100, 100:1100:100] = 60
        a[1200:1212, 100:700] = 30     # one line of text
        self.assertFalse(is_blank(Image.fromarray(a)))

    def test_review_footer_only_back_is_not_blank(self):
        # Finding 5: "Page 2 of 2" on an otherwise empty A4 back at 300 dpi.
        a = np.full((3508, 2480), 250, np.uint8)
        for x in range(1100, 1400, 30):
            a[3300:3330, x:x + 18] = 30
        self.assertFalse(is_blank(Image.fromarray(a)))

    def creased_back(self):
        rng = np.random.default_rng(2)
        a = np.clip(250 + rng.normal(0, 3, (3456, 2494)), 0, 255).astype(np.uint8)
        for i in range(13):        # a slightly slanted crease, 13 rows thick, broken like a real one
            a[1140 + i, i * 190:(i + 1) * 190 - 20] = 150
        for i in range(10):        # a second, fainter crease lower down
            a[2300 + i, 40 + i * 240:40 + (i + 1) * 240 - 30] = 165
        for y, x in ((400, 300), (2000, 1900), (3000, 800)):
            a[y:y + 3, x:x + 3] = 90  # specks of dust
        return Image.fromarray(a)

    def test_blank_back_with_fold_creases_is_blank(self):
        # From the first real letter: a blank back whose only marks are the letter's fold lines.
        self.assertTrue(is_blank(self.creased_back()))

    def test_full_width_fine_print_is_not_a_crease(self):
        # A single line of 6 pt legal text across the page must keep the page.
        a = np.full((3456, 2494), 250, np.uint8)
        for x in range(150, 2340, 14):
            a[3200:3218, x:x + 9] = 40
        self.assertFalse(is_blank(Image.fromarray(a)))

    def test_nearly_empty_page_is_not_flagged_skewed(self):
        # The angle finder has nothing to go on; an unsure angle mustn't raise a skew flag.
        self.assertNotIn("skewed", page_flags(self.creased_back()))

    def test_page_flags_edge(self):
        a = np.full((1600, 1200), 250, np.uint8)
        for y in range(400, 800, 40):
            a[y:y + 12, 600:1200] = 30  # lines of text running off the right edge
        self.assertEqual(page_flags(Image.fromarray(a)), ["edge"])
        self.assertEqual(page_flags(text_page().convert("L")), [])


@unittest.skipUnless(shutil.which("tesseract"), "tesseract not installed")
class UprightTest(unittest.TestCase):
    def test_upright_turns_upside_down_text(self):
        page = Image.new("L", (2480, 1600), 255)
        d = ImageDraw.Draw(page)
        font = ImageFont.load_default(size=60)
        for i in range(12):
            d.text((150, 100 + i * 110), "The quick brown fox jumps over the lazy dog", fill=0, font=font)
        _, rot, conf = upright(page.rotate(180))
        self.assertEqual(rot, 180)
        self.assertGreater(conf, 2.0)


if __name__ == "__main__":
    unittest.main()
