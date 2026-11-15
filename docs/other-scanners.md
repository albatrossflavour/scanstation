# Other scanners

scanstation is built and tested on a Fujitsu ScanSnap S1500 (and the S1500M, which is the same hardware). It MIGHT work with other document scanners that SANE supports and that have a scan button SANE can read. I only have the S1500, so anything else is untested. If you try another model, please open an issue saying what happened, whether it worked or not.

## What's specific to the S1500

All of it is in `scanstation/scanner.py`.

### Finding the scanner

```python
S1500_USB = ("04c5", "11a2")
```

`device()` runs `scanimage -L` and picks the first device matching `fujitsu:...S1500...`. Another Fujitsu model needs that pattern widened. A scanner on a different SANE backend also needs that backend added to `/etc/scanstation/sane.d/dll.conf`, which setup currently writes as `fujitsu` only.

`reset_usb()` uses the USB vendor and product id to find the scanner when it's stopped answering. Other models may never need it, and if they do, their ids go here.

### The scan options

```python
OPTIONS = [
    "--source", "ADF Duplex", "--mode", "Color", "--resolution", "300",
    "--page-width", "221.121", "--page-height", "876.695", "--ald=yes",
    "--swdeskew=no", "--swcrop=no", "--swskip", "0",
    "--df-action", "Stop", "--df-length=yes", "--format=tiff",
]
```

Several of these are `fujitsu` backend options: `--ald` (automatic length detection), `--swdeskew`, `--swcrop`, `--swskip`, and the `--df-*` double-feed options. Run `scanimage -d <device> -A` to see what your scanner offers. The page width and height are the S1500's maximum paper size.

The image pipeline expects raw, uncropped, colour, 300 dpi duplex pages with the scanner's backing visible around the paper, because it does its own straightening and cropping. A scanner that can't turn its own cropping off will probably still work, but the crop and edge checks are tuned for the S1500's backing.

### The button

`button_pressed()` reads the `--scan` option from `scanimage -A`, which the `fujitsu` backend reports as `[yes]` while the button is down. Other backends expose buttons differently, or not at all. A scanner with no readable button would need another trigger, such as a GPIO button on the Pi.

### Errors

`scan()` recognises a jam or double feed from SANE's "jammed" and "double feed" messages, and an empty feeder from "out of documents". Other backends may word these differently.

## Making a model work

The shape that would fit best is a small table of supported models in `scanner.py`, each with its USB id, device pattern, and scan options, chosen when the scanner is found. If you get another model working, a pull request along those lines, with what you tested, would be great.
