"""Assemble processed pages into a PDF, each page embedded as one JPEG at 300 dpi."""
QUALITY = 90


def build_pdf(pages, out):
    """Write pages (RGB or L) to out, a path or a binary file. Each page is encoded to JPEG once."""
    first, rest = pages[0], pages[1:]
    first.save(out, "PDF", save_all=True, append_images=rest, resolution=300, quality=QUALITY)
