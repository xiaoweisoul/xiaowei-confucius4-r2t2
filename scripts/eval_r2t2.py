#!/usr/bin/env python3
"""R2T2 (Confucius4-R2T2) 准确率评测：与 hojo eval_run.py 完全相同的口径。

复用 /mnt/asr/hojo_asr_multi_v1/eval_dataset/manifest_eval.json 与其 CER 计算
（normalize_text + 字级编辑距离），把 funasr 换成 R2T2 的 WebSocket
(/asr_stream_api_v1)。对照基准是 eval_results/stream_funasr.md（2026-09-06，
funasr 小微协议整段推流的结果），保证两者可比。

R2T2 协议要点（区别于 funasr）：
1. 第一条消息必须是 JSON header，secret_key 用官方调试值 "test0102"。
2. 音频按 160ms int16 PCM 块发送；结尾发字符串 EOS 触发收尾。
3. 返回的 msg.text 是增量片段，需要全部拼接。

用法：
  /mnt/asr/r2t2_test/venvs/r2t2/bin/python eval_r2t2.py [--limit N] [--concurrency K]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import time
import wave
from pathlib import Path
from typing import Any

import numpy as np
import websockets

R2T2_WS = "ws://127.0.0.1:18272/asr_stream_api_v1"
EVAL_ROOT = Path("/mnt/asr/hojo_asr_multi_v1/eval_dataset")
MANIFEST = EVAL_ROOT / "manifest_eval.json"
OUT_DIR = Path("/mnt/asr/r2t2_test/eval_results")
CHUNK_MS = 160
SAMPLE_RATE = 16000
EOS = "YOUDAO_ONETIME_ASR_STREAM_EOS"

# 与 hojo eval_run.py 保持同一规范化口径（去标点、数字转中文），
# 否则两边 CER 不可比。场景例：参考文本"26度"与识别"二十六度"
# 必须先归一化再比，否则数字形态差异会被当成替换错误。
PUNCT_RE = re.compile(r"[\s\u3000]+")
CN_PUNCT = "，。！？、；：“”‘’（）《》〈〉【】…—·,!?;:().~\"'-_"
DIGIT_MAP = {"0": "零", "1": "一", "2": "二", "3": "三", "4": "四",
             "5": "五", "6": "六", "7": "七", "8": "八", "9": "九"}


def normalize_text(text: str) -> str:
    out = []
    for ch in text or "":
        if ch in CN_PUNCT or ch.isspace() or ch == "\u3000":
            continue
        out.append(DIGIT_MAP.get(ch, ch))
    return "".join(out)


def edit_distance(a: str, b: str) -> int:
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def cer(ref: str, hyp: str) -> tuple[float, int, int, int]:
    r, h = normalize_text(ref), normalize_text(hyp)
    if not r:
        return (0.0, 0, len(h), 0)
    d = edit_distance(r, h)
    return (d / len(r), d, len(r), len(h))


def load_pcm_int16(path: Path) -> np.ndarray:
    with wave.open(str(path)) as w:
        assert w.getframerate() == 16000 and w.getnchannels() == 1 and w.getsampwidth() == 2, path
        raw = w.readframes(w.getnframes())
    return np.frombuffer(raw, dtype=np.int16)


def group_key(item: dict[str, Any]) -> str:
    noise, snr = item.get("noise"), item.get("snr_db")
    if noise is None:
        return "clean" if item.get("text") else "pure_noise"
    if snr is None:
        return f"{noise}_pure"
    return f"{noise}_{snr}db"


async def r2t2_transcribe(audio_path: Path) -> str:
    audio = load_pcm_int16(audio_path)
    block = int(SAMPLE_RATE * CHUNK_MS / 1000)
    texts: list[str] = []

    async with websockets.connect(R2T2_WS, ping_interval=None, max_size=10 * 1024 * 1024) as ws:
        await ws.send(json.dumps({
            "channels": 1,
            "sample_rate": SAMPLE_RATE,
            "requestId": str(time.time_ns()),
            # 不传 "zhen"(服务端映射为 None 自动检测):v1 路由的 no_reset
            # 解码在模型输出缺 <asr_text> 标签时丢弃整轮文本,噪声组实测
            # white_5db 13/30 条整句为空。显式 Chinese → 输出按纯文本处理,
            # 直连已验证完整识别。业务上中文场景固定 Chinese 也合理。
            "language": "Chinese",
            "use_vad": False,
            "secret_key": "test0102",
            "mode": "slow",
        }))

        async def sender() -> None:
            # 推流节奏:按音频实际时长等速发送(±2ms 整流),EOS 前留 0.3s 缓冲。
            # 为什么不能一口气猛灌:服务端 processor 是同步推理,每 160ms 块推理
            # 约 17~30ms,猛灌时 recv_queue 积压几百毫秒;此时发 EOS 会直接走
            # finish_streaming_transcribe,而该函数在 buffer 空时返回旧文本
            # (r2t2_asr.py:533),积压音频对应的 LSP 尾部 token 全部丢失——实测
            # white_5db 有 13/30 条整句为空。等速推流让服务端消费与发送同步,
            # EOS 时队列基本排空,结果与直连模型一致。
            pace = CHUNK_MS / 1000
            for i in range(0, len(audio), block):
                await ws.send(audio[i:i + block].tobytes())
                await asyncio.sleep(max(0, pace - 0.002))
            # 尾部补 1.5s 静音:LSP 会把最后 1~2 个 token 留在未稳定区,
            # 需要足够多的后续推理轮次让它们"稳定提交"
            await ws.send(np.zeros(int(SAMPLE_RATE * 1.5), dtype=np.int16).tobytes())
            await asyncio.sleep(0.3)
            await ws.send(EOS)

        async def reader() -> None:
            try:
                while True:
                    msg = await ws.recv()
                    j = json.loads(msg)
                    m = j.get("msg", {})
                    if isinstance(m, dict) and m.get("text"):
                        texts.append(m["text"])
            except websockets.ConnectionClosed:
                pass

        await asyncio.gather(sender(), reader())
    return "".join(texts)


async def run(items: list[dict[str, Any]], concurrency: int) -> list[dict[str, Any]]:
    sem = asyncio.Semaphore(concurrency)

    async def one(item: dict[str, Any]) -> dict[str, Any]:
        async with sem:
            hyp = await r2t2_transcribe(Path(item["path"]))
        c, d, lr, lh = cer(item["text"], hyp)
        return {"path": item["path"], "group": group_key(item),
                "ref": item["text"], "hyp": hyp,
                "cer": c, "dist": d, "len_ref": lr, "len_hyp": lh}

    return await asyncio.gather(*(one(x) for x in items))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--concurrency", type=int, default=4)
    args = ap.parse_args()

    items = json.loads(MANIFEST.read_text(encoding="utf-8"))
    if args.limit:
        items = items[: args.limit]

    t0 = time.time()
    results = asyncio.run(run(items, args.concurrency))
    elapsed = time.time() - t0

    # 分组聚合：CER = Σ编辑距离 / Σ参考字数；幻觉 = 纯噪声组的平均输出字数
    groups: dict[str, dict[str, Any]] = {}
    for r in results:
        g = groups.setdefault(r["group"], {"n": 0, "dist": 0, "len_ref": 0, "halluc": 0})
        g["n"] += 1
        g["dist"] += r["dist"]
        g["len_ref"] += r["len_ref"]
        if r["group"] == "pure_noise":
            g["halluc"] += len(normalize_text(r["hyp"]))

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out_json = OUT_DIR / "r2t2.json"
    out_json.write_text(json.dumps(
        {"results": results, "elapsed_sec": round(elapsed, 1)},
        ensure_ascii=False, indent=1), encoding="utf-8")

    lines = ["# R2T2 评测（离线整段推流口径）",
             f"- 时间: {time.strftime('%Y-%m-%dT%H:%M:%S')}",
             f"- 端点: {R2T2_WS}", f"- 条数: {len(results)}  耗时: {elapsed:.0f}s",
             "", "| 分组 | 条数 | CER | 幻觉字/条 |", "| --- | ---: | ---: | ---: |"]
    for g in sorted(groups):
        v = groups[g]
        cer_v = v["dist"] / v["len_ref"] if v["len_ref"] else 0
        hal = v["halluc"] / v["n"] if g == "pure_noise" else 0
        lines.append(f"| {g} | {v['n']} | {cer_v * 100:.2f}% | {hal:.1f} |")
    md = "\n".join(lines)
    (OUT_DIR / "r2t2.md").write_text(md, encoding="utf-8")
    print(md)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
