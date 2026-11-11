# mp4-srt

[English](README.md) | **简体中文**

用 FunASR 给本地 MP4 视频生成外挂 SRT 字幕。默认模型面向普通话，英文场景见[语言支持](#语言支持)。脚本抽取音轨，跑 VAD、Paraformer 语音识别和标点恢复，把字幕写在视频旁边。视频流不重新编码，也不烧录硬字幕。

## 处理流程

1. `ffmpeg` 从 MP4 抽出 16kHz 单声道 PCM WAV。
2. FunASR 依次运行 `fsmn-vad`、`paraformer-zh`、`ct-punc`。
3. 生成器把带标点的文稿切成句子，按 VAD 句子时间戳对齐，再把字符映射到时间。
4. 单条字幕目标为 42 个有效字、9 秒；过短的碎片并入相邻字幕后，可能略微超出其中一项。
5. SRT 使用 UTF-8 BOM 和 CRLF 换行。
6. 视频所在目录生成 CSV 索引，记录视频、目录、时长、字幕条数和生成时间。

## 语言支持

默认模型 `paraformer-zh` 和 `ct-punc` 面向普通话。中文讲课、演讲效果较好；英文识别质量会明显下降。

英文或多语言音频可以改用 [OpenAI Whisper](https://github.com/openai/whisper) 或 [faster-whisper](https://github.com/SYSTRAN/faster-whisper) 做转写，再把带时间戳的片段交给本项目的 SRT 组装逻辑。当前脚本直接调用 FunASR，没有 Whisper 开关。需要替换 `transcribe()` 并适配片段格式，或者用 Whisper 单独转写后只复用 SRT 生成部分。

## 环境要求

- Python 3.10 以上，实测 3.11。
- `ffmpeg` 和 `ffprobe` 在 `PATH` 中。
- 推荐 CUDA GPU。CPU 也能跑，速度慢很多。
- FunASR 1.4.13，以及与 CUDA 版本匹配的 PyTorch。
- 三个本地模型：`paraformer-zh`、`fsmn-vad`、`ct-punc`。

## 安装

```bash
git clone https://github.com/yanyintingyou/mp4-srt.git
cd mp4-srt
python -m venv .venv
```

激活虚拟环境：

```bash
# Windows
.venv\Scripts\activate
# Linux / macOS
source .venv/bin/activate
```

先按 https://pytorch.org/get-started/locally/ 安装对应 CUDA 版本的 PyTorch，再执行：

```bash
pip install -r requirements.txt
```

## 模型

从 ModelScope 下载三个模型，放在同一个目录下：

```text
models/funasr/
├─ paraformer-zh/
├─ fsmn-vad/
└─ ct-punc/
```

对应的 ModelScope 模型 ID：

| 目录 | 模型 |
| --- | --- |
| `paraformer-zh` | `iic/speech_paraformer-large_asr_nat-zh-cn-16k-common-vocab8404-pytorch` |
| `fsmn-vad` | `iic/speech_fsmn_vad_zh-cn-16k-common-pytorch` |
| `ct-punc` | `iic/punc_ct-transformer_zh-cn-common-vocab272727-pytorch` |

配置里的 `model_dir` 指向 `models/funasr` 这一层。

## 配置

```bash
cp config/funasr.example.json config/funasr.json
cp config/hotwords.example.txt config/hotwords.txt   # 可选
```

`model_dir` 填相对路径时，以仓库根目录为基准。模型放在别处或需要换设备时，改 `config/funasr.json`。

热词表一行一个词，拼好后传给 Paraformer。示例文件是通用财务、会计术语，可以直接替换成自己的领域词。

## 用法

单个视频：

```bash
python scripts/generate_srt.py --video "path/to/video.mp4"
```

整个目录：

```bash
python scripts/generate_srt.py --course-dir "path/to/course" --limit 0
```

`--limit` 默认是 `1`，改成 `0` 处理全部待处理视频。已有非空 SRT 默认跳过，中断后重跑同一条命令就是续跑。

常用参数：

| 参数 | 作用 |
| --- | --- |
| `--video PATH` | 处理单个 MP4 |
| `--course-dir PATH` | 递归扫描目录 |
| `--video-id ID[,ID...]` | 只处理指定 video_id |
| `--limit N` | 最多处理 N 个待处理视频；`0` 表示全部 |
| `--overwrite` | 重做已有 SRT |
| `--keep-audio` | 保留临时 WAV |
| `--keep-raw` | 保留 ASR 原始 JSON |
| `--no-index` | 不写索引，供并行 worker 使用 |
| `--shard-index N` | 并行分片编号 |
| `--shard-count N` | 并行分片总数 |
| `--rebuild-index` | 扫描已有 SRT，重建 CSV 索引 |

索引根目录通过向上查找 `章节索引.csv` 确定；没有该文件时用视频所在目录。也可以用 `--course-root` 显式指定。

## 输出

SRT 与 MP4 同目录同名：

```text
videos/
├─ chapter-01/
│  ├─ 01_123456_video-title.mp4
│  └─ 01_123456_video-title.srt
└─ 字幕索引.csv
```

索引列为 `课程,章目录,video_id,视频名,MP4相对路径,SRT相对路径,时长秒,字幕条数,状态,生成时间`，UTF-8 带 BOM、全字段加引号、CRLF 换行。运行状态存放在 `work/state/`。

## 并行处理

每个 worker 会加载一份完整模型。16GB 显存上，4 个 worker 是比较实用的默认值；3 个余量更大。5 个能跑，但已接近上限。

启动 N 个不同分片的 worker，结束后统一重建索引：

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

分片在过滤已有 SRT 之后进行。不要在别的 worker 跑完后单独补一个分片，那时待处理列表已经缩小，分配结果会变。要么同时启动全部分片，要么剩下的串行跑完。

## 中间文件

成功生成一个字幕后，对应的 WAV 和 ASR 原始 JSON 立即删除；失败时保留，便于排查。临时 SRT 无论成败都会删除。需要保留中间文件时加 `--keep-audio` 或 `--keep-raw`。

## 说明

- 识别结果会有少量错字和同音字。本项目不附带逐词替换表，讲课上下文通常足够推断原意。
- `punctuation timestamps could not be aligned, falling back to VAD segments` 是 FunASR 的无害提示，时间轴仍由 VAD 段落提供。
- 字幕内容来自源视频。只处理自己有权使用的内容，不要传播字幕或视频。
- 工具只读本地文件，不处理 DRM、账号权限或下载。

## 许可证

MIT，见 [LICENSE](LICENSE)。
