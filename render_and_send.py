#!/usr/bin/env python3
"""
Render + send  — step 2 of the daily pipeline
=============================================
Takes the dated HTML that fetch_compute.py produced, screenshots the five Story
frames at 1080x1920, and sends them to your Telegram as an album for review.

    python render_and_send.py                 # today
    python render_and_send.py --date 2026-07-01
    python render_and_send.py --no-send        # just make the PNGs, don't send
    python render_and_send.py --dry-run        # print the caption/plan, touch nothing

Needs (only when actually sending):
    TELEGRAM_BOT_TOKEN   from @BotFather
    TELEGRAM_CHAT_ID     your chat id (message the bot, then read getUpdates)
"""

import argparse
import datetime as dt
import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(HERE, "out")


def screenshot_frames(html_path, frame_dir):
    """Open the rendered HTML and grab each #frame-N as an exact 1080x1920 PNG."""
    from playwright.sync_api import sync_playwright  # lazy import: dry-run needs no browser
    os.makedirs(frame_dir, exist_ok=True)
    paths = []
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_context(
            viewport={"width": 1080, "height": 1920},
            device_scale_factor=1,          # 1x -> exact 1080x1920 pixels
            reduced_motion="reduce",        # freeze the wave so output is deterministic
        ).new_page()
        page.goto("file://" + html_path, wait_until="networkidle")
        page.evaluate("document.fonts.ready")   # wait for Google Fonts before capturing
        page.wait_for_timeout(200)
        for i in range(1, 6):
            out = os.path.join(frame_dir, f"frame{i}.png")
            page.locator(f"#frame-{i}").screenshot(path=out)
            paths.append(out)
        browser.close()
    return paths


def build_caption(data):
    idx, stock = data["index"], data["stock"]
    day = data["meta"]["dateLine"].split(" \u00b7 ")[0]
    arrow = "\u25b2" if stock["changeDir"] == "up" else "\u25bc"
    return (f"\U0001F30A <b>Mumbai Water Index</b> \u2014 {day}\n"
            f"{idx['score']}/10 \u00b7 {idx['tag']}\n"
            f"Stock {stock['pctCapacity']}%  {arrow}{stock['changeML']:,} ML today\n"
            f"Review, then post to Stories \U0001F447")


def send_media_group(frame_paths, caption, token, chat_id):
    """Send the 5 PNGs as one album of documents (uncompressed, so the files stay crisp)."""
    import requests
    url = f"https://api.telegram.org/bot{token}/sendMediaGroup"
    media, files, handles = [], {}, []
    for i, path in enumerate(frame_paths, start=1):
        key = f"frame{i}"
        fh = open(path, "rb")
        handles.append(fh)
        files[key] = (f"frame{i}.png", fh, "image/png")
        item = {"type": "document", "media": f"attach://{key}"}
        if i == 1:
            item["caption"] = caption
            item["parse_mode"] = "HTML"
        media.append(item)
    try:
        r = requests.post(url, data={"chat_id": chat_id, "media": json.dumps(media)},
                          files=files, timeout=90)
        if not r.ok:
            print(f"  Telegram API error {r.status_code}: {r.text}")
        r.raise_for_status()
        if not r.json().get("ok"):
            raise RuntimeError(f"Telegram error: {r.text}")
    finally:
        for fh in handles:
            fh.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date")
    ap.add_argument("--no-send", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    date = (dt.datetime.strptime(args.date, "%Y-%m-%d").date() if args.date else dt.date.today())
    iso = date.strftime("%Y-%m-%d")
    html_path = os.path.join(OUT_DIR, f"{iso}.html")
    json_path = os.path.join(OUT_DIR, f"{iso}.json")
    frame_dir = os.path.join(OUT_DIR, iso)

    with open(json_path, encoding="utf-8") as f:
        data = json.load(f)
    caption = build_caption(data)

    if args.dry_run:
        print("CAPTION:\n" + caption)
        print("\nWould screenshot ->", os.path.join(frame_dir, "frame[1-5].png"))
        print("Would send album to Telegram chat:", os.environ.get("TELEGRAM_CHAT_ID", "<TELEGRAM_CHAT_ID>"))
        return

    print("Screenshotting frames\u2026")
    frames = screenshot_frames(html_path, frame_dir)
    print("  wrote", ", ".join(os.path.basename(p) for p in frames))

    if args.no_send:
        print("--no-send set; stopping before Telegram.")
        return

    token = os.environ["TELEGRAM_BOT_TOKEN"]
    chat_id = os.environ["TELEGRAM_CHAT_ID"]
    print("Sending to Telegram\u2026")
    send_media_group(frames, caption, token, chat_id)
    print("  sent \u2713")


if __name__ == "__main__":
    main()
