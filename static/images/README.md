# Brand assets

Drop your Nova Connect logo here. The app looks for these filenames in order:

1. `nova-connect.png`  — preferred
2. `nova-connect.jpg`  — JPEG fallback
3. `nova-connect-mark.svg` — built-in SVG (used if neither raster file is present)

The CSS applies `filter: brightness(0) invert(1)` to whatever image loads, which
converts any monochrome logo (dark on transparent, navy on white, etc.) into a
clean white silhouette suitable for the dark sidebar.

## Recommended source

For the crispest result at any zoom level, use a transparent-background PNG at
512×512 or larger. Square aspect works best for the sidebar mark; the file is
displayed at 26 × 30 pixels.

If your logo PNG has a *white* background (it'll display as a white square
on the dark sidebar), open the CSS at `static/style.css` and remove the
`filter:` line on `.sidebar-brand .brand-mark`. Then either:

- export a transparent-background version from your design tool, OR
- the navy logo will display as-is on whatever background the sidebar has
  (you'll likely want to wrap the img in a white badge).

## Files

- `nova-connect-mark.svg`     — extracted N mark only (fallback)
- `nova-connect-wordmark.svg` — N mark + "nova connect" wordmark (unused in app, kept for reference)
- `nova-connect.svg`          — original full logo file copied from the workspace
