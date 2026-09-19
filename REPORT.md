# Confucius4-R2T2 测试部署与评测结论(2026-09-19)

对比基线:本机 serving 中的 funasr_nano_2512(端口 10095,未受任何影响)。

## 1. 结论速览

**R2T2 全面优于现网 funasr 配置**:识别精度约 2~3 倍提升,首字延迟从 ~1.67s 降到
~0.4s,纯噪声幻觉为 0(funasr 为 6.6 字/条)。但要商用需先过许可证审查,且官方
ws_server.py 有 3 个必须先修的缺陷(见第 5 节)。

## 2. 精度对比(312 条同一评测集,口径与 funasr 基准完全一致)

| 分组 | R2T2 CER | funasr CER(2026-09-06 基准) |
| --- | ---: | ---: |
| clean | **1.38%** | 3.22% |
| babble_5db | **2.07%** | 4.83% |
| babble_10db | **1.38%** | 4.14% |
| babble_20db | **3.45%** | 4.37% |
| pink_5db | **2.07%** | 4.14% |
| pink_10db | **4.14%** | 3.45% |
| pink_20db | **2.07%** | 3.45% |
| white_5db | **4.60%** | 4.14% |
| white_10db | **4.14%** | 4.14% |
| white_20db | **2.07%** | 3.45% |
| 纯噪声幻觉 | **0 字/条** | 6.6 字/条 |

注:"小微"会被识别成同音的"小威"(60 条含小微,31 条错)。已验证 R2T2 的
`system_prompt` 上下文提示可修正(传"唤醒词是「小微」。"后正确输出"小微"),
等价于 funasr 的热词功能,且 funasr 现网热词文件实际为空、并没有解决此问题。

## 3. 延迟对比(160ms 块,实测)

| 指标 | R2T2 | funasr 现网 |
| --- | ---: | ---: |
| 出字滞后(chunk 内 asr_cost) | P50 21~38ms | - |
| 首字延迟(语音开始后) | ~0.4s(320ms 首块+160ms 步进) | P50 1.67s |
| 输出模式 | 真流式,增量追加 | 伪流式,completed 后整句给 |

R2T2 服务端单轮推理 17~30ms(160ms 块),远小于块长,支持继续加并发。
GPU 占用 14.2G(util 0.20),与 funasr(10G)同卡共存无冲突。

## 4. 部署位置(全部独立于 funasr_nano_2512)

- 模型:`/mnt/asr/confucius4-r2t2/models/Confucius4-R2T2`(3.9G,ModelScope)
- VAD:`/mnt/asr/confucius4-r2t2/models/vad/Stream-VAD`(FireRedVAD)
- venv:`/mnt/asr/confucius4-r2t2/venvs/r2t2`(复用 funasr venv 的 torch 2.11+cu130
  + vllm 0.23.0 site-packages,再装 qwen-asr/fireredvad/sanic 小包)
- 代码:`/tmp/r2t2_repo`(github 克隆 + 本地补丁,见第 5 节)
- 服务:端口 18272,启动命令在 `logs/ws_server_start_cmd.txt` 风格见下:

```bash
cd /mnt/asr/confucius4-r2t2/run && CUDA_VISIBLE_DEVICES=0 PYTHONPATH=/tmp/r2t2_repo \
nohup /mnt/asr/confucius4-r2t2/venvs/r2t2/bin/python -u /tmp/r2t2_repo/ws_server.py \
  -p 18272 -m /mnt/asr/confucius4-r2t2/models/Confucius4-R2T2 \
  --vad_model_path /mnt/asr/confucius4-r2t2/models/vad/Stream-VAD \
  > /mnt/asr/confucius4-r2t2/logs/ws_server.log 2>&1 &
```

- 评测脚本:`/mnt/asr/confucius4-r2t2/eval_r2t2.py`(与 hojo eval_run.py 同口径)
- 延迟探针:`/mnt/asr/confucius4-r2t2/r2t2_latency_probe.py`
- 结果:`/mnt/asr/confucius4-r2t2/eval_results/r2t2.{md,json}`

## 5. 官方代码缺陷(切换前必须处理)

评测过程踩了 3 个坑,根因都在官方 `ws_server.py`/`r2t2` 包,已在 `/tmp/r2t2_repo`
本地修复或绕过:

1. **v1 路由 language=None 时丢整轮文本**:`streaming_transcribe_no_reset` 在
   模型输出无 `<asr_text>` 标签且未显式指定语言时,把该轮解码文本直接清空
   (r2t2_asr.py:743)。噪声下频发,white_5db 有 13/30 条整句为空。
   **绕过**:客户端 header 显式传 `"language": "Chinese"`。中文场景本就该固定。
2. **EOS 收尾 token 预算过小**:`first_max_new_tokens = max(1, 320/1280) = 1`,
   收尾只解码 1 个 token,LSP 未提交尾部丢失(实测丢整段尾巴)。
   **已修**:ws_server.py EOS 路径改为 `max(8, ...)`(两处)。
3. **每轮 token 预算 floor 过小**:160ms 块下 `int(step/1280)=2`,中文翻倍后
   仅 4,首 token 犹豫时文本"起不来"。**已修**:floor 4→8(两处),并补回被
   误删的 `max_new_tokens` 初始化行。

另注意:`/asr_stream_api_v1` 与 v0 是两条解码代码路径(v1=streaming_transcribe_
no_reset),官方 README 未说明差异;demo 素材走的是清晰音频,掩盖了上述问题。

## 6. 风险与待办

- **License**:代码 Apache-2.0,但模型权重是网易自定义协议(MODEL_LICENSE),
  商用前必须法务审查。funasr 全家桶是 Apache-2.0。
- 方言/口音:R2T2 未宣称方言能力,funasr-nano 官方支持 7 大方言。若有方言
  需求需补测。
- 说话人分离:R2T2 无 diarization,funasr 现网带 eres2netv2。若业务依赖需保留
  funasr 侧或另配。
- 服务为手动 nohup,重启机器不会自启;确认切流后再做 systemd。
