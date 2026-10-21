"""Time-lapse recorder for the agt Web console.

Usage: uv run --with playwright python scripts/record.py [--every 3] [--name run1]

Reads the console link from recording/console.log (written by scripts/console.sh),
opens it in headless Chrome and saves a screenshot every N seconds to
recording/<name>/frames/. Write a conversation id to recording/focus to switch the
view to that conversation; write a page name (agents, resources, interactions) to
switch to that page (chat, agents, observer, interactions, resources, settings). Stop with Ctrl+C or by creating recording/stop.
"""
import argparse
import re
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

root = Path(__file__).resolve().parent.parent / "recording"
pages = {name: name for name in ("chat", "agents", "observer", "interactions", "resources", "settings")}


def console_url():
    text = (root / "console.log").read_text()
    match = re.search(r"Open:\s+(\S+)", text)
    if not match:
        raise SystemExit("no console link in recording/console.log; run scripts/console.sh first")
    return match.group(1)


def apply_focus(page, focus):
    if focus in pages:
        page.evaluate(f"document.querySelector('[data-page=\"{pages[focus]}\"]')?.click()")
    else:
        page.evaluate("id => selectConversation(id)", focus)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--every", type=float, default=3.0)
    parser.add_argument("--name", default=time.strftime("%Y%m%d-%H%M%S"))
    parser.add_argument("--width", type=int, default=1600)
    parser.add_argument("--height", type=int, default=1000)
    args = parser.parse_args()
    frames = root / args.name / "frames"
    frames.mkdir(parents=True, exist_ok=True)
    focus_file, stop_file = root / "focus", root / "stop"
    stop_file.unlink(missing_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome", headless=True, args=["--accept-lang=zh-CN"])
        page = browser.new_page(viewport={"width": args.width, "height": args.height}, locale="zh-CN")
        page.goto(console_url())
        page.wait_for_function("document.querySelector('#connection')?.textContent?.length > 0")
        focus, n = None, len(list(frames.glob("*.png")))
        try:
            while not stop_file.exists():
                wanted = focus_file.read_text().strip() if focus_file.exists() else None
                if wanted and wanted != focus:
                    try:
                        apply_focus(page, wanted)
                        focus = wanted
                    except Exception as error:  # keep recording even if a focus id is wrong
                        print(f"focus {wanted!r} failed: {error}")
                page.screenshot(path=str(frames / f"{n:06d}.png"))
                n += 1
                time.sleep(args.every)
        except KeyboardInterrupt:
            pass
        browser.close()
    print(f"{n} frames in {frames}")


if __name__ == "__main__":
    main()
