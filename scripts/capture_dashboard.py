"""Screenshot the dashboard in light and dark mode.

    python scripts/capture_dashboard.py --url http://127.0.0.1:8010 --out docs/images

Used to verify the dashboard renders in both colour schemes, and to produce the images the
README embeds. It waits for the live chart to have drawn a real path before shooting, so a
screenshot cannot accidentally capture the loading state and be mistaken for the product.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

VIEWPORT = {"width": 1440, "height": 1200}


def capture(url: str, out_dir: Path, prefix: str, chart_only: bool) -> int:
    from playwright.sync_api import sync_playwright

    out_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []

    with sync_playwright() as p:
        browser = p.chromium.launch()
        for scheme in ("light", "dark"):
            page = browser.new_context(
                viewport=VIEWPORT, color_scheme=scheme, device_scale_factor=2
            ).new_page()
            errors: list[str] = []
            # Bound as a default argument: a bare closure over `errors` would capture the
            # loop variable, so the dark-mode pass would append into the light-mode list.
            page.on(
                "console",
                lambda m, sink=errors: sink.append(m.text) if m.type == "error" else None,
            )
            page.on("pageerror", lambda e, sink=errors: sink.append(str(e)))

            page.goto(url, wait_until="networkidle")
            # The chart polls on its own timer, so wait for it to have drawn rather than for
            # a fixed sleep: an empty <svg> screenshots just as happily as a full one.
            page.wait_for_function(
                "document.querySelectorAll('#stream path.line').length > 0", timeout=30_000
            )
            page.wait_for_timeout(2500)  # let one more poll land so the line has moved

            if errors:
                print(f"  JS errors in {scheme} mode:", file=sys.stderr)
                for e in errors:
                    print(f"    {e}", file=sys.stderr)
                return 1

            path = out_dir / f"{prefix}-{scheme}.png"
            page.screenshot(path=str(path), full_page=True)
            written.append(path)

            if chart_only:
                chart = page.locator("section.card", has=page.locator("#stream"))
                close = out_dir / f"{prefix}-chart-{scheme}.png"
                chart.screenshot(path=str(close))
                written.append(close)

            lines = page.evaluate("document.querySelectorAll('#stream path.line').length")
            dots = page.evaluate("document.querySelectorAll('#stream circle.ep-dot').length")
            rate = page.evaluate("document.getElementById('rate').textContent")
            print(f"  {scheme}: {lines} channel lines, {dots} episode markers, rate {rate.strip()}")
            page.close()
        browser.close()

    for path in written:
        print(f"  wrote {path} ({path.stat().st_size // 1024} KB)")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8010")
    parser.add_argument("--out", type=Path, default=Path("docs/images"))
    parser.add_argument("--prefix", default="dashboard-live")
    parser.add_argument(
        "--chart-only",
        action="store_true",
        help="also capture a close-up of the live chart card",
    )
    args = parser.parse_args(argv)
    return capture(args.url, args.out, args.prefix, args.chart_only)


if __name__ == "__main__":
    raise SystemExit(main())
