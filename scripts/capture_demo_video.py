#!/usr/bin/env python3
"""Record the project demo video against the disposable seeded server.

Produces:
  docs/assets/demo-video.mp4   full walkthrough (~2 minutes, 12 fps)
  docs/assets/demo-teaser.gif  short looping preview (dashboard + search)

Everything shown is seeded fixture data; no crawler runs, no real data.
Requires: playwright (already a dev dependency) and imageio-ffmpeg (dev-only,
installed via `pip install imageio-ffmpeg`; NOT part of requirements.txt).
"""
from __future__ import annotations

import shutil
import sys
import tempfile
import threading
import time
import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

OUT_DIR = ROOT / "docs" / "assets"
WIDTH, HEIGHT = 1920, 1080
FPS = 15


def load_seed():
    spec = importlib.util.spec_from_file_location("ct_e2e", ROOT / "tests" / "test_e2e_browser.py")
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module._seed


def boot(tmp: Path):
    import uvicorn
    from server.app import create_app
    db = tmp / "demo.db"
    load_seed()(db)
    app = create_app(db_path=str(db))
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(200):
        if server.started:
            break
        time.sleep(0.05)
    port = server.servers[0].sockets[0].getsockname()[1]
    return server, thread, f"http://127.0.0.1:{port}"


def main() -> int:
    from playwright.sync_api import sync_playwright
    import imageio_ffmpeg

    frames_dir = Path(tempfile.mkdtemp(prefix="ct-demo-frames-"))
    print("frames ->", frames_dir)

    with tempfile.TemporaryDirectory(prefix="ct-demo-server-") as tmp:
        server, thread, base = boot(Path(tmp))
        try:
            with sync_playwright() as pw:
                browser = pw.chromium.launch(headless=True)
                context = browser.new_context(
                    viewport={"width": WIDTH, "height": HEIGHT},
                    locale="en-US", timezone_id="Asia/Shanghai",
                    reduced_motion="reduce", device_scale_factor=1)
                page = context.new_page()
                page.set_default_timeout(20_000)

                stop = threading.Event()
                idx = {"n": 0}

                def grabber():
                    while not stop.is_set():
                        idx["n"] += 1
                        try:
                            page.screenshot(
                                path=str(frames_dir / f"f{idx['n']:05d}.png"),
                                clip={"x": 0, "y": 0, "width": WIDTH, "height": HEIGHT})
                        except Exception:
                            pass
                        time.sleep(1.0 / FPS)

                grabber_thread = threading.Thread(target=grabber, daemon=True)
                grabber_thread.start()

                def hold(seconds: float) -> None:
                    time.sleep(seconds)

                def goto(url: str, settle: float = 1.4) -> None:
                    page.goto(base + url)
                    page.wait_for_load_state("domcontentloaded")
                    hold(settle)

                # S1 — Dashboard: attention first
                goto("/dashboard", 2.0)
                hold(2.5)
                page.mouse.wheel(0, 420); hold(2.2)
                page.mouse.wheel(0, 420); hold(2.0)

                # S2 — Trials search: type a query
                goto("/trials", 1.2)
                box = page.locator("#global-trial-search")
                box.click(); hold(0.4)
                box.type("myocarditis", delay=90); hold(1.6)
                page.keyboard.press("Enter"); hold(2.6)
                hold(1.5)

                # S3 — Trial detail: tabs + changes
                goto("/trials/NCT/NCT00000001", 2.2)
                page.get_by_role("tab", name="Changes").click(); hold(2.6)
                page.get_by_role("tab", name="History").click(); hold(2.0)

                # S4 — Monitors + runs
                goto("/monitors", 1.6); hold(1.2)
                goto("/monitors/1", 1.8)
                page.get_by_role("tab", name="Runs").click(); hold(2.4)

                # S5 — Project workspace
                pid = page.request.post(base + "/api/projects", data={
                    "name": "Myocarditis watch", "description": "Follow the myocarditis corpus."
                }).json()["project"]["id"]
                page.request.post(f"{base}/api/projects/{pid}/assets/trials",
                                  data={"source": "NCT", "trial_id": "NCT09000301"})
                page.request.post(f"{base}/api/projects/{pid}/notes", data={
                    "body": "Check the CAR-T myocarditis arm at the next review."})
                goto(f"/projects/{pid}", 1.8)
                page.get_by_role("tab", name="Trials").click(); hold(1.8)
                page.get_by_role("tab", name="Notebook").click(); hold(2.6)

                # S6 — Briefing
                goto("/briefing", 2.0)
                page.mouse.wheel(0, 500); hold(2.0)

                # S7 — Data sources
                goto("/data-sources", 1.8)
                hold(1.4)

                # S8 — Dark + Chinese
                page.evaluate("localStorage.setItem('ct-theme','dark')")
                goto("/dashboard", 1.8); hold(2.0)
                page.evaluate("localStorage.setItem('ct-theme','light'); localStorage.setItem('ct-lang','zh')")
                goto("/trials", 1.6); hold(1.6)
                box = page.locator("#global-trial-search")
                box.click(); box.type("心肌炎", delay=110); hold(1.2)
                page.keyboard.press("Enter"); hold(2.6)
                page.evaluate("localStorage.setItem('ct-lang','en')")
                hold(1.0)

                stop.set()
                grabber_thread.join(timeout=10)
                context.close()
                browser.close()
        finally:
            server.should_exit = True
            thread.join(timeout=10)

    frame_files = sorted(frames_dir.glob("f*.png"))
    if len(frame_files) < FPS * 10:
        print(f"too few frames captured: {len(frame_files)}", file=sys.stderr)
        return 1
    print(f"captured {len(frame_files)} frames")

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    mp4_path = OUT_DIR / "demo-video.mp4"
    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    import subprocess
    # Feed frames to ffmpeg at the target fps (input rate = FPS, output 12 fps).
    cmd = [ffmpeg, "-y", "-loglevel", "error",
           "-framerate", str(FPS), "-i", str(frames_dir / "f%05d.png"),
           "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "23",
           "-vf", f"scale={WIDTH}:{HEIGHT}", "-movflags", "+faststart",
           str(mp4_path)]
    subprocess.run(cmd, check=True)
    print(mp4_path, f"{mp4_path.stat().st_size/1e6:.1f} MB")

    # GIF teaser: first ~26 s (dashboard + search) at half resolution, 8 fps.
    gif_path = OUT_DIR / "demo-teaser.gif"
    teaser_frames = frame_files[: int(FPS * 18)]
    palette = frames_dir / "palette.png"
    subprocess.run([ffmpeg, "-y", "-loglevel", "error",
                    "-framerate", str(FPS), "-i", str(frames_dir / "f%05d.png"),
                    "-frames:v", str(len(teaser_frames)),
                    "-vf", f"fps=10,scale={WIDTH*2//3}:-2:flags=lanczos,palettegen",
                    str(palette)], check=True)
    subprocess.run([ffmpeg, "-y", "-loglevel", "error",
                    "-framerate", str(FPS), "-i", str(frames_dir / "f%05d.png"),
                    "-i", str(palette),
                    "-lavfi", f"fps=10,scale={WIDTH*2//3}:-2:flags=lanczos[x];[x][1:v]paletteuse",
                    "-frames:v", str(len(teaser_frames)),
                    str(gif_path)], check=True)
    print(gif_path, f"{gif_path.stat().st_size/1e6:.1f} MB")

    shutil.rmtree(frames_dir, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
