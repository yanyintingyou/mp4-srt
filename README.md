# mp4-srt

English | [简体中文](README.zh-CN.md)

Generate sidecar SRT subtitles for local MP4 videos with FunASR. Any video with speech works. The script extracts the audio track, runs voice activity detection, Paraformer ASR, and punctuation restoration, then writes an SRT next to each video. The video stream stays untouched.

## How it works

1. `ffmpeg` extracts a 16 kHz mono PCM WAV from the MP4.
2. FunASR runs `fsmn-vad`, `paraformer-zh`, and `ct-punc` on the whole recording.
3. The builder splits the punctuated transcript into sentences, aligns each sentence to the VAD sentence timestamps, and maps characters to time.
4. Cues target 42 effective characters and 9 seconds. Very short cues merge into a neighbour, which can push a merged cue slightly past either limit.
5. The SRT is written as UTF-8 with BOM and CRLF line endings.
6. A course-level CSV index records the video, chapter directory, duration, cue count, and generation time.

## Requirements

- Python 3.10 or newer. Tested with 3.11.
- `ffmpeg` and `ffprobe` on `PATH`.
- A CUDA GPU is recommended. CPU inference works but is much slower.
- FunASR 1.4.13 and a PyTorch build that matches your CUDA version.
- Three local FunASR models: `paraformer-zh`, `fsmn-vad`, `ct-punc`.

## Install

```bash
git clone https://github.com/<your-account>/mp4-srt.git
cd mp4-srt
python -m venv .venv
```

Activate the environment:

```bash
# Windows
.venv\Scripts\activate
# Linux / macOS
source .venv/bin/activate
```

Install PyTorch for your CUDA version first. See https://pytorch.org/get-started/locally/. Then:

```bash
pip install -r requirements.txt
```

## Models

Download the three models from ModelScope and place them under one directory:

```text
models/funasr/
├─ paraformer-zh/
├─ fsmn-vad/
└─ ct-punc/
```

ModelScope model IDs:

| Directory | Model |
| --- | --- |
| `paraformer-zh` | `iic/speech_paraformer-large_asr_nat-zh-cn-16k-common-vocab8404-pytorch` |
| `fsmn-vad` | `iic/speech_fsmn_vad_zh-cn-16k-common-pytorch` |
| `ct-punc` | `iic/punc_ct-transformer_zh-cn-common-vocab272727-pytorch` |

`model_dir` in the config points to the parent `models/funasr` directory.

## Configure

```bash
cp config/funasr.example.json config/funasr.json
cp config/hotwords.example.txt config/hotwords.txt   # optional
```

Relative `model_dir` values resolve from the repository root. Edit `config/funasr.json` if your models live elsewhere or you need a different device.

`config/hotwords.txt` accepts one term per line. These terms are passed to Paraformer as hotwords. The example list contains general finance and accounting vocabulary. Remove it or replace it with terms from your own domain.

## Usage

Single video:

```bash
python scripts/generate_srt.py --video "path/to/video.mp4"
```

A whole course directory:

```bash
python scripts/generate_srt.py --course-dir "path/to/course" --limit 0
```

`--limit` defaults to `1`. Set it to `0` to process every pending video. Existing non-empty SRT files are skipped, so rerunning the same command resumes the batch.

Useful options:

| Option | Meaning |
| --- | --- |
| `--video PATH` | Process one MP4 |
| `--course-dir PATH` | Scan a course directory recursively |
| `--video-id ID[,ID...]` | Restrict a course run to selected video IDs |
| `--limit N` | Process at most N pending videos; `0` means all |
| `--overwrite` | Regenerate existing SRT files |
| `--keep-audio` | Keep the temporary WAV |
| `--keep-raw` | Keep the raw ASR JSON |
| `--no-index` | Do not write the index; used by parallel workers |
| `--shard-index N` | Worker index for parallel runs |
| `--shard-count N` | Total number of parallel shards |
| `--rebuild-index` | Rebuild the CSV index from existing SRT files |

The course root is detected by walking up from the video until it finds `章节索引.csv`. Use `--course-root` to set it explicitly.

## Output

The SRT sits next to the MP4 and uses the same base name:

```text
course/
├─ chapter-01/
│  ├─ 01_123456_video-title.mp4
│  └─ 01_123456_video-title.srt
└─ 字幕索引.csv
```

The index columns are `课程,章目录,video_id,视频名,MP4相对路径,SRT相对路径,时长秒,字幕条数,状态,生成时间`. The file is UTF-8 with BOM, quoted, and CRLF terminated. Runtime state lives in `work/state/`.

## Parallel processing

Each worker loads its own copy of the models. On a 16 GB GPU, four workers are a practical default. Three leave more VRAM headroom. Five can work but sits close to the limit.

Start N workers with different shard indexes, then rebuild the index once:

```bash
n=4
for i in $(seq 0 $((n - 1))); do
  python scripts/generate_srt.py \
    --course-dir "path/to/course" --limit 0 --no-index \
    --shard-index "$i" --shard-count "$n" &
done
wait
python scripts/generate_srt.py --rebuild-index --course-dir "path/to/course"
```

Sharding happens after existing SRT files are filtered out. Do not launch a missing shard later against a reduced pending list. Restart all workers together, or run the remainder sequentially.

## Cleanup

On success, the temporary WAV and raw ASR JSON are deleted. On failure, both are kept for diagnosis. The temporary SRT is always removed. Use `--keep-audio` or `--keep-raw` when you need the intermediates.

## Notes

- ASR output contains occasional wrong words and homophones. This project does not ship a word replacement table. The surrounding speech usually makes the meaning clear.
- `punctuation timestamps could not be aligned, falling back to VAD segments` is a harmless FunASR warning. Timing comes from VAD segments.
- SRT content comes from the source video. Process only material you have the right to use. Do not redistribute subtitles or video content.
- The tool reads local files only. It does not handle DRM, account access, or downloads.

## License

MIT. See [LICENSE](LICENSE).
