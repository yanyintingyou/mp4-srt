#!/usr/bin/env python3
"""FunASR 讲课文稿转 SRT。单视频默认，批量需显式 --limit 0。"""
from __future__ import annotations

import argparse
import csv
import json
import math
import os
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WORK = ROOT / "work"
CONFIG_PATH = ROOT / "config" / "funasr.json"
HOTWORDS_PATH = ROOT / "config" / "hotwords.txt"

STRIP_RE = re.compile(r"[\s\u3000。！？；，、：:；;,.!?\"'“”‘’（）()《》〈〉【】\[\]{}<>—…·]")
SENT_RE = re.compile(r"[^。！？；!?;]+[。！？；!?;]?")
COMMA_RE = re.compile(r"(?<=[，、])")


def log(msg: str) -> None:
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


def core(text: str) -> str:
    return STRIP_RE.sub("", text or "")


def load_config() -> dict:
    cfg = json.loads(CONFIG_PATH.read_text("utf-8"))
    lines = []
    if HOTWORDS_PATH.exists():
        lines = [x.strip() for x in HOTWORDS_PATH.read_text("utf-8").splitlines()]
    cfg["hotword"] = " ".join(x for x in lines if x)
    return cfg


def run(cmd: list[str], timeout: int | None = None) -> subprocess.CompletedProcess:
    p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)
    if p.returncode != 0:
        tail = (p.stderr or p.stdout or "").strip()[-1200:]
        raise RuntimeError(f"{Path(cmd[0]).name} exit {p.returncode}: {tail}")
    return p


def probe_duration(video: Path, ffprobe: str) -> float:
    p = run([ffprobe, "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", str(video)])
    return float(p.stdout.strip())


def extract_audio(video: Path, audio: Path, ffmpeg: str) -> None:
    audio.parent.mkdir(parents=True, exist_ok=True)
    run([ffmpeg, "-y", "-v", "error", "-i", str(video), "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(audio)])


def parse_video(video: Path) -> tuple[str, str, str]:
    m = re.match(r"^(\d{2})_(\d+)_(.+)\.mp4$", video.name)
    if m:
        return m.group(2), m.group(3), video.parent.name
    return video.stem, video.stem, video.parent.name


def fmt_ts(ms: float) -> str:
    n = max(0, int(round(ms)))
    h, n = divmod(n, 3600000)
    m, n = divmod(n, 60000)
    s, n = divmod(n, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{n:03d}"


def build_char_times(segs: list[dict], duration_ms: float) -> tuple[str, list[float], list[float]]:
    chars: list[str] = []
    starts: list[float] = []
    ends: list[float] = []
    for seg in segs:
        t = str(seg.get("text") or "")
        s = float(seg.get("start") or 0)
        e = float(seg.get("end") or 0)
        n = len(t)
        if n == 0:
            continue
        for i, ch in enumerate(t):
            chars.append(ch)
            starts.append(s + (e - s) * i / n)
            ends.append(s + (e - s) * (i + 1) / n)
    raw = "".join(chars)
    if raw:
        return raw, starts, ends
    return "", [], []


def split_chunks(text: str, max_chars: int) -> list[str]:
    if len(core(text)) <= max_chars:
        return [text]
    pieces = [x for x in COMMA_RE.split(text) if x]
    out: list[str] = []
    buf = ""
    for p in pieces:
        if len(core(buf + p)) <= max_chars:
            buf += p
            continue
        if buf:
            out.append(buf)
        buf = p
        while len(core(buf)) > max_chars:
            out.append(buf[:max_chars])
            buf = buf[max_chars:]
    if buf:
        out.append(buf)
    return out


def split_by_time(text: str, start: float, end: float, max_cue_ms: float, max_chars: int) -> list[tuple[float, float, str]]:
    if end - start <= max_cue_ms and len(core(text)) <= max_chars:
        return [(start, end, text)]
    pieces = split_chunks(text, max_chars)
    total = sum(len(core(p)) for p in pieces) or 1
    out: list[tuple[float, float, str]] = []
    cur = start
    for p in pieces:
        share = (end - start) * len(core(p)) / total
        p_end = cur + share
        if p_end - cur > max_cue_ms:
            n = math.ceil((p_end - cur) / max_cue_ms)
            step = max(1, math.ceil(len(p) / n))
            subparts = [p[i:i + step] for i in range(0, len(p), step)]
            total_sub = sum(len(core(x)) for x in subparts) or len(subparts)
            cur2 = cur
            for sp in subparts:
                d = (p_end - cur) * len(core(sp)) / total_sub
                out.append((cur2, cur2 + d, sp))
                cur2 += d
        else:
            out.append((cur, p_end, p))
        cur = p_end
    return out


def merge_short_cues(cues: list[dict]) -> list[dict]:
    out: list[dict] = []
    i = 0
    while i < len(cues):
        c = cues[i]
        short = (c["end"] - c["start"] < 800) or (len(core(c["text"])) <= 2)
        if short and i + 1 < len(cues) and cues[i + 1]["start"] - c["end"] < 1500:
            nxt = cues[i + 1]
            nxt["start"] = c["start"]
            nxt["text"] = c["text"] + nxt["text"]
            i += 1
            continue
        if short and out and c["start"] - out[-1]["end"] < 1500:
            out[-1]["end"] = c["end"]
            out[-1]["text"] += c["text"]
        else:
            out.append(c)
        i += 1
    return out


def build_cues(text: str, raw_text: str, segs: list[dict], duration_ms: float, max_chars: int, max_cue_ms: float) -> list[dict]:
    text = (text or "").strip()
    if not text:
        return []
    raw, starts, ends = build_char_times(segs, duration_ms)
    if not raw:
        raw = core(raw_text) or core(text)
        n = len(raw)
        if n == 0:
            return []
        starts = [duration_ms * i / n for i in range(n)]
        ends = [duration_ms * (i + 1) / n for i in range(n)]

    cues: list[dict] = []
    cursor = 0
    for sent in SENT_RE.findall(text):
        sent = sent.strip()
        if not sent:
            continue
        for chunk in split_chunks(sent, max_chars):
            c = core(chunk)
            if not c:
                continue
            idx = raw.find(c, cursor)
            if idx < 0:
                idx = raw.find(c)
            if idx < 0:
                idx = min(max(0, cursor), max(0, len(raw) - 1))
            end_idx = min(len(raw), idx + len(c))
            if end_idx <= idx:
                continue
            s = starts[idx]
            e = ends[end_idx - 1]
            cursor = end_idx
            if e <= s:
                e = s + 300
            cues.extend(split_by_time(chunk, s, e, max_cue_ms, max_chars))

    cues = [{"start": s, "end": e, "text": t} for s, e, t in cues]
    cues = merge_short_cues(cues)
    out: list[dict] = []
    prev_end = 0.0
    for c in cues:
        s, e, t = c["start"], c["end"], c["text"]
        s = min(max(0.0, s), duration_ms)
        e = min(max(s + 200.0, e), duration_ms + 1000)
        if s < prev_end:
            s = prev_end
            if e <= s:
                e = s + 300
        out.append({"start": s, "end": e, "text": t.strip()})
        prev_end = e
    return out


def write_srt(cues: list[dict], path: Path) -> None:
    lines: list[str] = []
    for i, c in enumerate(cues, 1):
        lines += [str(i), f"{fmt_ts(c['start'])} --> {fmt_ts(c['end'])}", c["text"], ""]
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        f.write("\r\n".join(lines))


def validate_cues(cues: list[dict], duration_ms: float) -> list[str]:
    problems: list[str] = []
    if not cues:
        return ["no cues"]
    prev_end = -1.0
    for i, c in enumerate(cues, 1):
        if c["end"] <= c["start"]:
            problems.append(f"cue {i}: end<=start")
        if c["start"] + 1 < prev_end:
            problems.append(f"cue {i}: overlaps previous")
        if c["end"] - c["start"] > 15000:
            problems.append(f"cue {i}: duration > 15s")
        prev_end = c["end"]
    if cues[-1]["end"] < duration_ms - 30000:
        problems.append(f"last cue ends {round((duration_ms-cues[-1]['end'])/1000)}s before video end")
    return problems


def srt_stats(path: Path) -> tuple[int, float]:
    text = path.read_text("utf-8-sig", errors="replace")
    times = re.findall(r"(\d{2}):(\d{2}):(\d{2}),(\d{3}) --> (\d{2}):(\d{2}):(\d{2}),(\d{3})", text)
    if not times:
        return 0, 0.0
    h, m, s, ms = map(int, times[-1][4:])
    return len(times), (h * 3600 + m * 60 + s) * 1000 + ms


def detect_course_root(video: Path) -> Path:
    p = video.parent
    for _ in range(4):
        if (p / "章节索引.csv").exists():
            return p
        p = p.parent
    return video.parent


def write_index(course_root: Path, state: dict) -> None:
    state_dir = WORK / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r'[\\/:*?"<>|]', "_", course_root.name)
    state_path = state_dir / f"{safe}.json"
    state_path.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")

    rows = sorted(state.values(), key=lambda x: str(x.get("SRT相对路径") or ""))
    cols = ["课程", "章目录", "video_id", "视频名", "MP4相对路径", "SRT相对路径", "时长秒", "字幕条数", "状态", "生成时间"]
    with (course_root / "字幕索引.csv").open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f, quoting=csv.QUOTE_ALL, lineterminator="\r\n")
        w.writerow(cols)
        for r in rows:
            w.writerow([r.get(k, "") for k in cols])


def update_index(course_root: Path, entry: dict) -> None:
    state_dir = WORK / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r'[\\/:*?"<>|]', "_", course_root.name)
    state_path = state_dir / f"{safe}.json"
    state = json.loads(state_path.read_text("utf-8")) if state_path.exists() else {}
    state[entry["video_id"]] = entry
    write_index(course_root, state)


def rebuild_index(course_dir: Path, cfg: dict, course_root: Path | None = None) -> None:
    course_root = course_root or course_dir
    state: dict = {}
    for video in sorted(course_dir.rglob("*.mp4")):
        srt = video.with_suffix(".srt")
        if not srt.exists() or srt.stat().st_size == 0:
            continue
        video_id, title, chapter = parse_video(video)
        cues, _ = srt_stats(srt)
        duration = probe_duration(video, cfg["ffprobe"])
        state[video_id] = {
            "课程": course_root.name, "章目录": chapter, "video_id": video_id, "视频名": title,
            "MP4相对路径": video.relative_to(course_root).as_posix(),
            "SRT相对路径": srt.relative_to(course_root).as_posix(),
            "时长秒": f"{duration:.3f}", "字幕条数": cues, "状态": "ok",
            "生成时间": datetime.fromtimestamp(srt.stat().st_mtime).isoformat(timespec="seconds"),
        }
    write_index(course_root, state)
    log(f"index rebuilt: {len(state)} entries -> {course_root / '字幕索引.csv'}")


def load_model(cfg: dict):
    from funasr import AutoModel
    base = Path(cfg["model_dir"])
    if not base.is_absolute():
        base = ROOT / base
    t = time.perf_counter()
    model = AutoModel(
        model=str(base / cfg["model"]),
        vad_model=str(base / cfg["vad_model"]),
        punc_model=str(base / cfg["punc_model"]),
        device=cfg["device"],
        disable_update=True,
        log_level="ERROR",
        vad_kwargs={"max_single_segment_time": int(cfg["max_single_segment_time"])},
    )
    log(f"model loaded in {time.perf_counter()-t:.1f}s")
    return model


def transcribe(model, audio: Path, cfg: dict) -> dict:
    kwargs = {
        "input": str(audio),
        "batch_size_s": int(cfg["batch_size_s"]),
        "sentence_timestamp": True,
        "return_raw_text": True,
    }
    if cfg.get("hotword"):
        kwargs["hotword"] = cfg["hotword"]
    t = time.perf_counter()
    res = model.generate(**kwargs)
    if not res:
        raise RuntimeError("FunASR returned empty result")
    log(f"asr {time.perf_counter()-t:.1f}s")
    return res[0]


def process_one(model, video: Path, cfg: dict, args) -> str:
    video_id, title, chapter = parse_video(video)
    srt = video.with_suffix(".srt")
    course_root = Path(args.course_root) if args.course_root else detect_course_root(video)
    rel_video = video.relative_to(course_root).as_posix()
    rel_srt = srt.relative_to(course_root).as_posix()
    duration = probe_duration(video, cfg["ffprobe"])

    if srt.exists() and srt.stat().st_size > 0 and not args.overwrite:
        cues, _ = srt_stats(srt)
        log(f"SKIP {video_id} existing srt ({cues} cues)")
        if not args.no_index:
            update_index(course_root, {
                "课程": course_root.name, "章目录": chapter, "video_id": video_id, "视频名": title,
                "MP4相对路径": rel_video, "SRT相对路径": rel_srt, "时长秒": f"{duration:.3f}",
                "字幕条数": cues, "状态": "existing", "生成时间": datetime.now().isoformat(timespec="seconds"),
            })
        return "skip"

    audio = WORK / "audio" / f"{video_id}.wav"
    raw_path = WORK / "raw" / f"{video_id}.json"
    tmp_srt = WORK / "tmp" / f"{video_id}.srt"
    ok = False
    try:
        log(f"START {video_id} {title}")
        extract_audio(video, audio, cfg["ffmpeg"])
        log(f"audio {audio.stat().st_size/1048576:.1f} MB")
        r = transcribe(model, audio, cfg)
        text = str(r.get("text") or "").strip()
        raw_text = str(r.get("raw_text") or "").strip()
        segs = r.get("sentence_info") or []
        raw_obj = {
            "video_id": video_id, "video": str(video), "duration": duration,
            "text": text, "raw_text": raw_text, "sentence_info": segs,
            "params": {k: cfg[k] for k in ("model", "vad_model", "punc_model", "device", "max_single_segment_time", "batch_size_s")},
            "created_at": datetime.now().isoformat(timespec="seconds"),
        }
        raw_path.parent.mkdir(parents=True, exist_ok=True)
        raw_path.write_text(json.dumps(raw_obj, ensure_ascii=False, indent=1), encoding="utf-8")
        cues = build_cues(text, raw_text, segs, duration * 1000, int(cfg["max_chars"]), float(cfg["max_cue_ms"]))
        if not cues:
            raise RuntimeError("no cues generated")
        problems = validate_cues(cues, duration * 1000)
        if problems:
            log("WARN " + "; ".join(problems[:5]))
        write_srt(cues, tmp_srt)
        os.replace(tmp_srt, srt)
        if not args.no_index:
            update_index(course_root, {
                "课程": course_root.name, "章目录": chapter, "video_id": video_id, "视频名": title,
                "MP4相对路径": rel_video, "SRT相对路径": rel_srt, "时长秒": f"{duration:.3f}",
                "字幕条数": len(cues), "状态": "ok", "生成时间": datetime.now().isoformat(timespec="seconds"),
            })
        log(f"OK {video_id} {len(cues)} cues, {len(text)} chars -> {srt}")
        ok = True
        return "ok"
    finally:
        if ok and not args.keep_audio:
            audio.unlink(missing_ok=True)
        if ok and not args.keep_raw:
            raw_path.unlink(missing_ok=True)
        tmp_srt.unlink(missing_ok=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--video", type=Path, help="单个 MP4 路径")
    g.add_argument("--course-dir", type=Path, help="课程根目录，扫描 MP4")
    ap.add_argument("--video-id", help="只处理指定 video_id，可用逗号分隔多个")
    ap.add_argument("--limit", type=int, default=1, help="批量上限；0 表示全部，默认 1")
    ap.add_argument("--course-root", type=Path, help="课程根目录，默认向上查找 章节索引.csv")
    ap.add_argument("--overwrite", action="store_true")
    ap.add_argument("--keep-audio", action="store_true")
    ap.add_argument("--keep-raw", action="store_true")
    ap.add_argument("--no-index", action="store_true", help="并行 worker 用，避免同时写索引")
    ap.add_argument("--shard-index", type=int, default=0)
    ap.add_argument("--shard-count", type=int, default=1)
    ap.add_argument("--rebuild-index", action="store_true", help="只扫描已有 SRT，重建索引")
    args = ap.parse_args()

    cfg = load_config()
    WORK.joinpath("audio").mkdir(parents=True, exist_ok=True)
    WORK.joinpath("raw").mkdir(parents=True, exist_ok=True)
    WORK.joinpath("tmp").mkdir(parents=True, exist_ok=True)

    if args.rebuild_index:
        if not args.course_dir:
            ap.error("--rebuild-index requires --course-dir")
        rebuild_index(args.course_dir, cfg, args.course_root)
        return 0

    if args.shard_count < 1 or not (0 <= args.shard_index < args.shard_count):
        ap.error("invalid shard index/count")

    if args.video:
        videos = [args.video]
    else:
        videos = sorted(args.course_dir.rglob("*.mp4"))
        if args.video_id:
            ids = {x.strip() for x in args.video_id.split(",") if x.strip()}
            videos = [v for v in videos if parse_video(v)[0] in ids]
        pending = [v for v in videos if args.overwrite or not v.with_suffix(".srt").exists()]
        if args.limit and args.limit > 0:
            pending = pending[:args.limit]
        videos = pending[args.shard_index::args.shard_count] if args.shard_count > 1 else pending

    if not videos:
        log("nothing to do")
        return 0

    model = load_model(cfg)
    ok = skip = fail = 0
    for v in videos:
        try:
            r = process_one(model, v, cfg, args)
            if r == "ok":
                ok += 1
            else:
                skip += 1
        except Exception as e:
            fail += 1
            log(f"FAIL {v.name}: {e}")
    log(f"DONE ok={ok} skip={skip} fail={fail}")
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
