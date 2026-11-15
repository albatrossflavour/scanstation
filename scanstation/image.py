"""Page processing: straighten and crop raw duplex scans, correct colour, turn pages upright.

The values here were fitted in a bake-off against the output of Fujitsu's ScanSnap Manager.
"""
import io
import re
import subprocess
import tempfile
from importlib import resources

import numpy as np
from PIL import Image

# --- straightening and cropping -------------------------------------------------------------


def _score_angles(img, angles):
    """Sharpness of the row profile at each angle (higher = straighter)."""
    g = np.asarray(img.convert("L").resize((img.width // 4, img.height // 4)), dtype=float)
    ink = Image.fromarray(((g < 160) * 255).astype(np.uint8))
    out = []
    for a in angles:
        r = np.asarray(ink.rotate(a, resample=Image.BILINEAR, expand=True, fillcolor=0), dtype=float)
        out.append(np.var(np.diff(r.sum(1))))
    return np.array(out)


def find_angle(img):
    """Rotation in degrees that straightens the text lines, and a confidence (peak over median)."""
    coarse = np.arange(-45, 45.01, 1.0)
    s = _score_angles(img, coarse)
    best = coarse[s.argmax()]
    fine = np.arange(best - 1, best + 1.001, 0.1)
    sf = _score_angles(img, fine)
    return float(fine[sf.argmax()]), float(s.max() / (np.median(s) + 1e-9))


def _backing(raw):
    """The scanner backing colour, from the outermost columns, which are always backing."""
    return np.median(np.concatenate([raw[:, :30].reshape(-1, 3), raw[:, -30:].reshape(-1, 3)]), axis=0)


CARD_MAX = (1300, 890)   # 110 x 75 mm at 300 dpi: anything smaller is treated as a card
CARD_MARGIN = 35         # 3 mm


def _box(mask):
    """Bounding box of the main object in a mask, ignoring isolated specks (smoothed profiles)."""
    def span(profile):
        profile = np.convolve(profile, np.ones(15) / 15, mode="same")
        on = np.flatnonzero(profile > 0.2)
        return (on[0], on[-1] + 1) if on.size else None
    xs = span(mask.mean(0))
    if not xs:
        return None
    ys = span(mask[:, xs[0]:xs[1]].mean(1))
    return (xs[0], ys[0], xs[1], ys[1]) if ys else None


def paper_box(img, threshold=12):
    """Bounding box of the paper: whatever differs from the scanner backing.

    The scan is always wider than the paper (page width is set to the maximum), so the outermost
    columns are backing. Their colour is the reference, which copes with the two sensors' different
    backings and with paper of any tint, thermal receipts included.
    """
    a = np.asarray(img.convert("RGB"), dtype=float)
    ref = _backing(a)
    differs = np.abs(a - ref).max(2) > threshold
    # Cards (licences, bank cards, tags) are where the trims below go wrong: a dark stripe near the
    # edge passes for a shadow, and the shadow rule eats into the card. For anything card-sized,
    # crop to everything that differs from the backing and keep a few mm spare instead.
    raw = _box(differs)
    if raw and (raw[2] - raw[0]) < CARD_MAX[0] and (raw[3] - raw[1]) < CARD_MAX[1]:
        m = CARD_MARGIN
        return (max(raw[0] - m, 0), max(raw[1] - m, 0), min(raw[2] + m, a.shape[1]), min(raw[3] + m, a.shape[0]))
    # A card's own shadow on the backing is a little darker than the backing and the same hue. It's
    # not paper, so drop it, or the crop keeps a drop-shadow band along two edges.
    # shortcut: this also trims about 4 mm off a card's sides; tune against more cards.
    lum, ref_lum = a.mean(2), ref.mean()
    hue_shift = np.abs((a - lum[..., None]) - (ref - ref_lum)).max(2)
    shadow = (lum < ref_lum) & (ref_lum - lum < 60) & (hue_shift < 15)
    differs &= ~shadow

    def longest_run(profile, cutoff=0.35, smooth=31):
        # Smooth first so a single noisy column on the backing can't start a run.
        profile = np.convolve(profile, np.ones(smooth) / smooth, mode="same")
        on = np.r_[False, profile > cutoff, False]
        edges = np.flatnonzero(np.diff(on.astype(int)))
        runs = [[s, e] for s, e in zip(edges[::2], edges[1::2])]
        if not runs:
            return 0, len(profile)
        # Bridge gaps up to 30 mm, so a full-width dark band (a header bar, a photo) doesn't split the page.
        merged = [runs[0]]
        for s, e in runs[1:]:
            if s - merged[-1][1] <= 350:
                merged[-1][1] = e
            else:
                merged.append([s, e])
        s, e = max(merged, key=lambda r: r[1] - r[0])
        return s, e

    x0, x1 = longest_run(differs.mean(0))
    # Rows: the scanner's own length detection already ends the scan at the paper, so only trim
    # inside the paper's columns, where backing shows above or below it.
    y0, y1 = longest_run(differs[:, x0:x1].mean(1))
    # The paper's leading and trailing edges throw a near-black band onto the backing, too dark for
    # the shadow rule above, sometimes with paper-bright backing beyond it on the back sensor. Look
    # for the band's near-black core within 60 rows (5 mm) of each end, and cut at its inner side.
    # A row of text never averages this dark across the page's width, so text near an edge survives.
    dark = lum[:, x0:x1].mean(1) < 110
    window = 60
    if y1 - y0 > 4 * window:
        tail = np.flatnonzero(dark[y1 - window:y1])
        if tail.size:
            y1 = y1 - window + tail[0]
        head = np.flatnonzero(dark[y0:y0 + window])
        if head.size:
            y0 = y0 + head[-1] + 1
    return x0, y0, x1, y1


def straighten_sheet(front, back):
    """Straighten both sides of a raw duplex sheet with one shared angle, then crop to the paper.

    The back is a mirror image of the front, so its skew is the negative of the front's. The more
    confident side's angle is used for both, which rescues a side that's mostly empty form boxes.
    """
    af, cf = find_angle(front)
    ab, cb = find_angle(back)
    angle = af if cf >= cb else -ab
    out = []
    for img, a in ((front, angle), (back, -angle)):
        # Fill rotated-in corners with this sensor's own backing colour, so they aren't taken for paper.
        rgb = img.convert("RGB")
        backing = tuple(int(v) for v in _backing(np.asarray(rgb)))
        r = rgb.rotate(a, resample=Image.BICUBIC, expand=True, fillcolor=backing)
        x0, y0, x1, y1 = paper_box(r)
        out.append(r.crop((x0 + 4, y0 + 4, x1 - 4, y1 - 4)))
    return out[0], out[1], {"front": (af, cf), "back": (ab, cb), "used": angle}


# --- colour ---------------------------------------------------------------------------------

_P = np.load(io.BytesIO(resources.files("scanstation.assets").joinpath("colour_simple.npz").read_bytes()))
LUMA = np.array([0.299, 0.587, 0.114])
WHITE, GREY_GAMMA, CHROMA, PAPER_TINT_LIMIT = 0.98, 1.3, 0.12, 0.10
# shortcut: a page with under 0.2% strongly coloured pixels is saved greyscale. Fitted on four documents
# with a thin margin (receipt 0.19%, rego form back 0.27%); revisit with more samples. Errs towards colour.
COLOUR_PIXEL, COLOUR_SHARE = 0.30, 0.002


def correct(img, side="front"):
    """Colour correction that can't shift hue. Returns an RGB page, or L for a colourless one.

    1. White-balance against the sensor's own paper white (front and back sensors differ).
    2. Neutralise slightly off-white paper (thermal receipts); clearly coloured paper keeps its colour.
    3. One shared brightness curve fitted to ScanSnap, applied as a ratio so hue is kept.
    4. A single saturation factor fitted to ScanSnap.
    5. Extra darkening for near-grey pixels only, so light print matches ScanSnap.
    """
    a = np.asarray(img.convert("RGB"), dtype=float) / (_P["wf"] if side == "front" else _P["wb"])
    y = np.clip(a, 0, 1.2)
    # Paper white comes from bright, near-neutral pixels only, so a coloured band (a licence's yellow
    # header) isn't mistaken for the paper.
    lum0 = y @ LUMA
    neutral = (y.max(2) - y.min(2)) < 0.12
    bright = y[neutral & (lum0 >= np.percentile(lum0[neutral], 70))] if neutral.any() else y.reshape(-1, 3)
    paper = np.percentile(bright, 60, axis=0)
    tint = paper / paper.mean()
    if np.abs(tint - 1).max() < PAPER_TINT_LIMIT:
        y = np.clip(y / tint, 0, 1.2)
    lum = y @ LUMA
    nl = np.interp(lum, _P["grid"], _P["curve"])
    y = y * (nl / np.maximum(lum, 1e-4))[..., None]
    y = nl[..., None] + float(_P["sat"]) * (y - nl[..., None])
    y = np.clip(y / WHITE, 0, 1)
    grey = np.clip(1 - (y.max(2) - y.min(2)) / CHROMA, 0, 1)[..., None]
    y = y ** (1 + (GREY_GAMMA - 1) * grey)
    out = Image.fromarray((y * 255).astype(np.uint8))
    if ((y.max(2) - y.min(2)) > COLOUR_PIXEL).mean() < COLOUR_SHARE:
        return out.convert("L")
    return out


# --- page checks ----------------------------------------------------------------------------

# shortcut: starting values; BLANK_PIXELS gets checked against a real blank back in the end-to-end test.
# Counted in pixels, not as a share of the page, so a footer on a full A4 back still counts as content.
BLANK_PIXELS, BLANK_BORDER = 400, 0.03
SKEW_LIMIT, SKEW_MIN_CONFIDENCE, EDGE_STRIP, EDGE_INK = 1.5, 3.0, 8, 0.02
# A fold: at most 16 px thick (a slight slant spreads it), across at least half the page, and sparse.
# Measured on a real letter's crease: 13 rows, 79% span, 9% dense.
CREASE_MAX_ROWS, CREASE_SPAN, CREASE_DENSITY = 16, 0.5, 0.15


def _dark(img):
    return np.asarray(img.convert("L")) < 200


def is_blank(img):
    """True for a corrected page with almost no ink.

    Ignores a border where edge shadows sit, and fold creases: a fold is a thin line right across
    the page, while writing sits in short clusters, so rows and columns dark across a quarter of
    the page are left out of the count.
    """
    d = _dark(img)
    by, bx = int(d.shape[0] * BLANK_BORDER), int(d.shape[1] * BLANK_BORDER)
    d = d[by:d.shape[0] - by, bx:d.shape[1] - bx].copy()
    _clear_creases(d)
    _clear_creases(d.T)
    return int(d.sum()) < BLANK_PIXELS


def _clear_creases(d):
    """Blank out fold lines in place: thin, sparse runs of rows, clean either side, spanning half the page.

    A line of text is taller and far denser where it sits, so it never qualifies, even fine print.
    """
    active = d.mean(1) > 0.005
    edges = np.flatnonzero(np.diff(np.r_[False, active, False].astype(int)))
    for start, end in zip(edges[::2], edges[1::2]):
        band = d[start:end]
        if end - start <= CREASE_MAX_ROWS and band.any(0).mean() >= CREASE_SPAN and band.mean() < CREASE_DENSITY:
            d[start:end] = False


def page_flags(img):
    """Reasons to hold a page: still skewed after straightening, or content touching an edge."""
    flags = []
    angle, confidence = find_angle(img)
    # With little text the angle finder is guessing, so an unsure angle isn't evidence of skew.
    if abs(angle) > SKEW_LIMIT and confidence >= SKEW_MIN_CONFIDENCE:
        flags.append("skewed")
    d = _dark(img)
    s = EDGE_STRIP
    if max(d[:s].mean(), d[-s:].mean(), d[:, :s].mean(), d[:, -s:].mean()) > EDGE_INK:
        flags.append("edge")
    return flags


# --- orientation ----------------------------------------------------------------------------

MIN_CONFIDENCE = 2.0  # below this tesseract is guessing; leave the page as it is


def orientation(img, run=subprocess.run):
    """Clockwise rotation tesseract says the page needs, and its confidence."""
    with tempfile.NamedTemporaryFile(suffix=".png") as f:
        img.convert("L").save(f.name)
        r = run(["tesseract", f.name, "-", "--psm", "0"], capture_output=True, text=True)
    out = r.stdout + r.stderr
    rot = re.search(r"Rotate: (\d+)", out)
    conf = re.search(r"Orientation confidence: ([\d.]+)", out)
    if not rot or not conf:
        return 0, 0.0
    return int(rot.group(1)), float(conf.group(1))


def upright(img):
    """Turn a page upright when tesseract is confident. Returns the page, rotation and confidence."""
    rot, conf = orientation(img)
    if rot and conf >= MIN_CONFIDENCE:
        img = img.rotate(-rot, expand=True)
    return img, rot, conf
