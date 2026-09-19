#!/usr/bin/env python3
# R2T2 延迟冒烟测试:模拟真实说话节奏(按音频时长等速推流),
# 测量每个增量文本片段的"说话结束到出字"延迟,并统计整体指标。
# 用法: python r2t2_latency_probe.py <wav> [ws_uri]
import asyncio, sys, time, json, uuid
import numpy as np
import soundfile as sf
import websockets

WAV = sys.argv[1] if len(sys.argv) > 1 else "/tmp/r2t2_repo/resources/test.wav"
URI = sys.argv[2] if len(sys.argv) > 2 else "ws://127.0.0.1:18272/asr_stream_api_v1"
SR = 16000
CHUNK_MS = 160
EOS = "YOUDAO_ONETIME_ASR_STREAM_EOS"

async def main():
    audio, sr = sf.read(WAV, dtype="int16")
    assert sr == SR, f"need 16k, got {sr}"
    dur = len(audio) / SR
    block = int(SR * CHUNK_MS / 1000)
    # 延迟记录: (音频位置秒, 收到文本, 单向耗时)
    events = []

    t0 = time.monotonic()

    async with websockets.connect(URI, ping_interval=None, max_size=None) as ws:
        # 协议要求第一条消息是 JSON header(服务端用它鉴权并读取采样率等),
        # secret_key "test0102" 是官方 ws_client.py 内置的调试密钥。
        await ws.send(json.dumps({
            "channels": 1,
            "sample_rate": SR,
            "requestId": str(uuid.uuid4()),
            # 服务端把 "zhen" 映射为 None(自动检测语言),而 v1 路由的
            # streaming_transcribe_no_reset 在模型输出缺少 <asr_text> 标签时
            # 会把整轮文本丢弃(噪声下频发,white_5db 有 13/30 条整句为空)。
            # 显式传 "Chinese" → force_language 生效,模型输出按纯文本处理,
            # 不再依赖 tag,直连已验证此参数下识别完整。
            "language": "Chinese",
            "use_vad": False,
            "secret_key": "test0102",
            "mode": "slow",
        }))

        async def sender():
            for i in range(0, len(audio), block):
                chunk = audio[i:i+block]
                if len(chunk) < block:
                    chunk = np.pad(chunk, (0, block - len(chunk)))
                await ws.send(chunk.tobytes())
                # 关键:按 160ms 真实节奏推流,模拟实时说话
                await asyncio.sleep(CHUNK_MS / 1000)
            await asyncio.sleep(0.5)  # 尾部静音,让 VAD/模型收尾
            await ws.send(EOS)

        async def receiver():
            try:
                while True:
                    msg = await ws.recv()
                    now = time.monotonic()
                    j = json.loads(msg)
                    m = j.get("msg", {})
                    if isinstance(m, dict):
                        txt = m.get("text", "")
                        cost_ms = m.get("total_cost_ms")
                    else:
                        # 服务端错误消息的 msg 是纯字符串,只打印不统计
                        print(f"[server] status={j.get('status')} msg={m}")
                        txt, cost_ms = "", None
                    if txt:
                        events.append({
                            "t_since_start": round(now - t0, 3),
                            "text": txt,
                            "asr_cost_ms": m.get("asr_cost_ms"),
                            "total_cost_ms": cost_ms,
                        })
            except websockets.ConnectionClosed:
                pass

        await asyncio.gather(sender(), receiver())

    print(f"audio_dur={dur:.2f}s  chunks={len(audio)//block + 1}")
    full = ""
    for e in events:
        full += e["text"]
        print(f"[{e['t_since_start']:7.3f}s] +{e['text']!r}  (asr_cost={e['asr_cost_ms']}ms)")
    print(f"\nFINAL: {full}")
    print(f"fragments={len(events)}")
    if events:
        costs = [e["total_cost_ms"] for e in events if e["total_cost_ms"] is not None]
        if costs:
            costs = sorted(costs)
            n = len(costs)
            print(f"server total_cost_ms: p50={costs[n//2]:.0f} p95={costs[int(n*0.95)]:.0f} max={costs[-1]:.0f}")

asyncio.run(main())
