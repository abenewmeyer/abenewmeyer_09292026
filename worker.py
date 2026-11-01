#!/usr/bin/env python3
"""Abe Shorts worker — generate Format-A-ish shorts → Supabase Storage + media_assets.
Self-healing: creates bucket if missing, retries TTS, skips existing slugs.
Env:
  SUPABASE_URL
  SUPABASE_SERVICE_ROLE_KEY
  BATCH_SIZE (default 20)
  WRITINGS_URL (default site index)
"""
from __future__ import annotations
import asyncio, json, os, random, re, subprocess, time, urllib.request
from datetime import datetime, timezone
from pathlib import Path

try:
    import edge_tts
except ImportError:
    subprocess.check_call([os.sys.executable, "-m", "pip", "install", "-q", "edge-tts", "Pillow", "supabase"])
    import edge_tts

from PIL import Image, ImageDraw, ImageFont
from supabase import create_client

SUPABASE_URL = os.environ["SUPABASE_URL"].rstrip("/")
SUPABASE_KEY = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
BATCH_SIZE = int(os.environ.get("BATCH_SIZE", "20"))
WRITINGS_URL = os.environ.get("WRITINGS_URL", "https://www.abenewmeyer.com/writings-index.json")
BUCKET = "media-shorts"
VOICE = "en-US-AvaNeural"
RATE = "-12%"
W, H = 720, 1280
GOLD, WHITE, DARK = (212, 175, 55), (250, 250, 252), (12, 14, 18)

WORK = Path(os.environ.get("WORK_DIR", "/tmp/abe_shorts_work"))
WORK.mkdir(parents=True, exist_ok=True)
BROLL = WORK / "broll"
BROLL.mkdir(exist_ok=True)

sb = create_client(SUPABASE_URL, SUPABASE_KEY)


def log(msg: str) -> None:
    print(msg, flush=True)


def ensure_ffmpeg() -> None:
    r = subprocess.run(["ffmpeg", "-version"], capture_output=True)
    if r.returncode != 0:
        subprocess.run(["apt-get", "-qq", "update"], check=False)
        subprocess.run(["apt-get", "-qq", "install", "-y", "ffmpeg"], check=False)


def ensure_bucket() -> None:
    try:
        names = [b.name for b in sb.storage.list_buckets()]
    except Exception as e:
        log(f"list_buckets: {e}")
        names = []
    if BUCKET not in names:
        try:
            sb.storage.create_bucket(BUCKET, options={"public": True})
            log(f"Created bucket {BUCKET}")
        except Exception as e:
            log(f"create_bucket note: {e}")


def ensure_table_row_works() -> None:
    try:
        sb.table("media_assets").select("slug").limit(1).execute()
    except Exception as e:
        raise SystemExit(
            "media_assets table missing. In Supabase SQL Editor run the CREATE TABLE from handoff, then re-run.\n"
            + str(e)
        )


def load_writings() -> list:
    path = WORK / "writings-index.json"
    urllib.request.urlretrieve(WRITINGS_URL, path)
    data = json.loads(path.read_text(encoding="utf-8"))
    log(f"Writings loaded: {len(data)}")
    return data


def done_slugs() -> set:
    out = set()
    start = 0
    while True:
        res = sb.table("media_assets").select("slug").range(start, start + 999).execute()
        rows = res.data or []
        if not rows:
            break
        out.update(r["slug"] for r in rows if r.get("slug"))
        if len(rows) < 1000:
            break
        start += 1000
    return out


def slugify(s: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-")
    return s[:80] or "short"


def font(size: int, bold: bool = False):
    path = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
    try:
        return ImageFont.truetype(path, size)
    except Exception:
        return ImageFont.load_default()


def wrap(draw, text, fnt, max_w):
    words, lines, cur = text.split(), [], ""
    for w in words:
        t = (cur + " " + w).strip()
        if draw.textbbox((0, 0), t, font=fnt)[2] <= max_w:
            cur = t
        else:
            if cur:
                lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines[:7]


def ensure_broll() -> list[Path]:
    files = list(BROLL.glob("*.jpg"))
    if len(files) >= 6:
        return files
    urls = [
        "https://images.pexels.com/photos/1001682/pexels-photo-1001682.jpeg?auto=compress&cs=tinysrgb&w=1280",
        "https://images.pexels.com/photos/346529/pexels-photo-346529.jpeg?auto=compress&cs=tinysrgb&w=1280",
        "https://images.pexels.com/photos/965225/pexels-photo-965225.jpeg?auto=compress&cs=tinysrgb&w=1280",
        "https://images.pexels.com/photos/1181676/pexels-photo-1181676.jpeg?auto=compress&cs=tinysrgb&w=1280",
        "https://images.pexels.com/photos/38136/pexels-photo-38136.jpeg?auto=compress&cs=tinysrgb&w=1280",
        "https://images.pexels.com/photos/374870/pexels-photo-374870.jpeg?auto=compress&cs=tinysrgb&w=1280",
    ]
    for i, u in enumerate(urls):
        dest = BROLL / f"b{i}.jpg"
        if dest.exists() and dest.stat().st_size > 5000:
            continue
        try:
            urllib.request.urlretrieve(u, dest)
        except Exception:
            Image.new("RGB", (1280, 720), (20 + i * 25, 40, 70)).save(dest, quality=90)
    return list(BROLL.glob("*.jpg"))


def make_frame(broll: Path, title: str, caption: str, idx: int, total: int) -> Image.Image:
    im = Image.open(broll).convert("RGB")
    r = W / H
    iw, ih = im.size
    if iw / ih > r:
        nw, nh = int(ih * r), ih
    else:
        nw, nh = iw, int(iw / r)
    x, y = (iw - nw) // 2, (ih - nh) // 2
    im = im.crop((x, y, x + nw, y + nh)).resize((W, H), Image.Resampling.LANCZOS)
    im = Image.blend(im, Image.new("RGB", (W, H), DARK), 0.42)
    draw = ImageDraw.Draw(im)
    draw.rectangle([0, 0, W, 8], fill=GOLD)
    ph = 340
    draw.rectangle([0, H - ph, W, H], fill=(8, 12, 20))
    draw.rectangle([0, H - ph, W, H - ph + 4], fill=GOLD)
    draw.text((40, 28), "ABE NEWMEYER", font=font(20, True), fill=GOLD)
    y0 = H - ph + 28
    if idx == 0:
        draw.text((40, y0), title[:48], font=font(15, True), fill=GOLD)
        y0 += 32
    for line in wrap(draw, caption, font(28, True), W - 80):
        draw.text((40, y0), line, font=font(28, True), fill=WHITE)
        y0 += 38
    return im


def beats(title: str, content: str) -> list[str]:
    lines = [ln.strip() for ln in (content or "").splitlines() if ln.strip()]
    if lines and title.lower() in lines[0].lower():
        lines = lines[1:]
    text = " ".join(lines)
    sents = [s.strip() for s in re.split(r"(?<=[.!?])\s+", text) if len(s.strip()) > 40]
    if not sents:
        sents = [title, "Show up. Notice what is true. Take the next honest step."]
    def clip(s, n=150):
        s = re.sub(r"\s+", " ", s).strip()
        return s if len(s) <= n else s[: n - 1].rsplit(" ", 1)[0] + "."
    picks = [sents[0], sents[len(sents) // 3], sents[(2 * len(sents)) // 3], sents[-1]]
    bridges = [
        "Consistency is quieter than breakthrough, and more durable.",
        "You do not need a perfect day to take the next right step.",
        "Notice what is real. Then move from there.",
    ]
    out = [clip(s) for s in picks]
    for b in bridges:
        if sum(len(x.split()) for x in out) < 150:
            out.insert(-1, b)
    return out[:7]


async def tts(text: str, path: Path, tries: int = 4) -> None:
    last = None
    for i in range(tries):
        try:
            await edge_tts.Communicate(text, VOICE, rate=RATE).save(str(path))
            if path.exists() and path.stat().st_size > 1000:
                return
        except Exception as e:
            last = e
            time.sleep(2 + i * 2)
    raise RuntimeError(f"TTS failed: {last}")


def duration(path: Path) -> float:
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
        capture_output=True, text=True,
    )
    try:
        return float(r.stdout.strip())
    except Exception:
        return 4.0


def upload(local: Path, remote: str) -> str:
    data = local.read_bytes()
    try:
        sb.storage.from_(BUCKET).remove([remote])
    except Exception:
        pass
    sb.storage.from_(BUCKET).upload(remote, data, {"content-type": "video/mp4"})
    return sb.storage.from_(BUCKET).get_public_url(remote)


async def render_one(w: dict, broll: list[Path]) -> tuple[str, float, str]:
    title = w["title"]
    slug = w.get("slug") or slugify(title)
    topics = w.get("topics") or []
    content = w.get("content") or ""
    parts = beats(title, content)
    work = WORK / slug
    work.mkdir(exist_ok=True)
    scenes = []
    chapters = []
    t0 = 0.0
    for i, beat in enumerate(parts):
        ap = work / f"a{i}.mp3"
        await tts(beat, ap)
        dur = max(3.2, duration(ap) + 0.3)
        frame = make_frame(broll[i % len(broll)], title, beat, i, len(parts))
        fp = work / f"f{i}.jpg"
        frame.save(fp, quality=92)
        sp = work / f"s{i}.mp4"
        subprocess.run(
            ["ffmpeg", "-y", "-loop", "1", "-i", str(fp), "-i", str(ap),
             "-c:v", "libx264", "-tune", "stillimage", "-pix_fmt", "yuv420p",
             "-c:a", "aac", "-b:a", "192k", "-shortest", "-movflags", "+faststart", str(sp)],
            capture_output=True, text=True, check=True,
        )
        scenes.append(sp)
        chapters.append({"index": i, "start": round(t0, 2), "end": round(t0 + dur, 2), "text": beat})
        t0 += dur
    lst = work / "list.txt"
    lst.write_text("".join(f"file '{s}'\n" for s in scenes))
    raw = work / "raw.mp4"
    r = subprocess.run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(lst), "-c", "copy", str(raw)], capture_output=True, text=True)
    if r.returncode != 0 or not raw.exists():
        subprocess.run(
            ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(lst), "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", str(raw)],
            capture_output=True, text=True, check=True,
        )
    final = work / f"{slug}.mp4"
    desc = f"Abe Newmeyer reflection: {title}."
    subprocess.run(
        ["ffmpeg", "-y", "-i", str(raw), "-c", "copy",
         "-metadata", f"title={title}", "-metadata", "artist=Abe Newmeyer",
         "-metadata", f"description={desc}", str(final)],
        capture_output=True, text=True, check=True,
    )
    dur = duration(final)
    url = upload(final, f"shorts/{slug}.mp4")
    row = {
        "slug": slug,
        "title": title,
        "description": desc,
        "topics": topics,
        "tags": list(dict.fromkeys(["Abe Newmeyer", "Be Greater"] + list(topics))),
        "duration_sec": round(dur, 2),
        "aspect": "9:16",
        "format": "A-semantic-broll",
        "video_path": f"shorts/{slug}.mp4",
        "video_url": url,
        "transcript": " ".join(c["text"] for c in chapters),
        "chapters": chapters,
        "status": "generated",
        "writing_slug": slug,
        "video_object": {
            "@type": "VideoObject",
            "name": title,
            "description": desc,
            "contentUrl": url,
            "uploadDate": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            "duration": f"PT{int(dur)}S",
        },
    }
    sb.table("media_assets").upsert(row, on_conflict="slug").execute()
    # cleanup work dir to save disk
    for p in work.glob("*"):
        try:
            p.unlink()
        except Exception:
            pass
    return slug, dur, url


async def main() -> None:
    ensure_ffmpeg()
    ensure_bucket()
    ensure_table_row_works()
    writings = load_writings()
    done = done_slugs()
    pending = [w for w in writings if (w.get("slug") or slugify(w.get("title", ""))) not in done]
    batch = pending[:BATCH_SIZE]
    log(f"Done={len(done)} Pending={len(pending)} Batch={len(batch)}")
    if not batch:
        log("QUEUE EMPTY — generation complete.")
        return
    broll = ensure_broll()
    ok = fail = 0
    for i, w in enumerate(batch, 1):
        title = w.get("title", "?")
        log(f"[{i}/{len(batch)}] {title}")
        try:
            slug, dur, url = await render_one(w, broll)
            log(f"  OK {dur:.0f}s {slug}")
            ok += 1
        except Exception as e:
            log(f"  FAIL {e}")
            fail += 1
            time.sleep(3)
    log(f"RUN COMPLETE ok={ok} fail={fail} remaining≈{max(0, len(pending)-ok)}")


if __name__ == "__main__":
    asyncio.run(main())
