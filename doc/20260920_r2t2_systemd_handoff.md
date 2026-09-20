# R2T2 ASR 部署与调用 — 阶段性交接说明（2026-09-20）

> 本文档是 R2T2 流式 ASR 从部署到接入评估全过程的阶段性交接记录，
> 供后续会话/协作者快速接管。服务端与调用协议均已实测验证。

## 1. 当前状态一句话

R2T2 流式 ASR 已从裸 nohup 切换为 **systemd 托管并实测可用**；
xiaowei-server 调用 FunASR 的方式已分析完毕，结论是 **R2T2 裸协议与
xiaowei 协议不兼容，需补适配层才能接入**。

## 2. 服务部署（已完成，验证通过）

- 服务：`r2t2-ws.service`，**enabled + active（running）**，`Restart=on-failure`
- 端口 `0.0.0.0:18272`，WebSocket 握手实测返回 101，完整识别实测通过
- 启动脚本：`/mnt/asr/confucius4-r2t2/repo/scripts/start_r2t2_ws.sh`
  （支持 `R2T2_*` 环境变量覆盖，参照 FunASR 的脚本模式）
- 单元文件：`/mnt/asr/confucius4-r2t2/repo/r2t2-ws.service`
  （已装到 `/etc/systemd/system/`，参照 `funasr-ws.service`）
- 日志：`/mnt/asr/confucius4-r2t2/logs/r2t2_ws.log`；
  每请求级日志在 `server/logs/requests/<requestId>.log`
- 常用命令：

```bash
systemctl status r2t2-ws
journalctl -u r2t2-ws -f
tail -f /mnt/asr/confucius4-r2t2/logs/r2t2_ws.log
```

## 3. 关键路径

| 用途 | 路径 |
| --- | --- |
| git 仓库（xiaoweisoul/xiaowei-confucius4-r2t2） | `/mnt/asr/confucius4-r2t2/repo`（当前在 feat/systemd-deploy 分支，已推送） |
| 运行代码（上游 clone + 本地修复，从 /tmp 迁来） | `/mnt/asr/confucius4-r2t2/server`（含 .git，ws_server.py 含本地修复） |
| 模型 | `/mnt/asr/confucius4-r2t2/models/Confucius4-R2T2`（3.9G） |
| VAD | `/mnt/asr/confucius4-r2t2/models/vad/Stream-VAD` |
| venv | `/mnt/asr/confucius4-r2t2/venvs/r2t2/bin/python`（python 3.12） |
| 评测报告 | `/mnt/asr/confucius4-r2t2/repo/REPORT.md`（必读，含缺陷清单） |

> ⚠️ 旧路径 `/mnt/asr/r2t2_test` **已不存在**（改名 confucius4-r2t2），
> 任何引用需用新路径。

## 4. R2T2 调用协议（实测验证）

```
ws://<host>:18272/asr_stream_api_v1
```

1. 第一帧发 JSON header：
   `{channels:1, sample_rate:16000, requestId:"uuid", language:"Chinese", use_vad:false, secret_key:"test0102", mode:"slow"}`
   - **language 必须显式 "Chinese"**，传 zhen/缺省会丢文本（服务端缺陷）
2. 收 `{"status":"connected"}` 确认
3. 发音频：16kHz 单声道 int16 PCM 块（160ms=2560 采样 OK，快速连发也 OK）
4. 末尾发 EOS 文本帧：`"YOUDAO_ONETIME_ASR_STREAM_EOS"`
5. 收消息：所有消息 `status` 都是 `"success"`，`msg.text` 是**增量需拼接**；
   **不能靠 status 判断结束，要等服务端主动关闭连接（code=1000）**
   （这是最容易踩的坑）

现成客户端：`server/ws_client.py`（注意默认 `language="zhen"`、端口 8272，用前要改）。
实测样本结果："之前有顾客自己带酒水也没加收钱或者不让喝"（6.74s 音频完整识别）。

## 5. xiaowei-server 接入分析（已完成，结论）

分析对象：`/mnt/xiaowei_2_0/xiaowei-server/internal/asr/funasr_xiaowei_realtime/`
（recognizer.go、stream.go、protocol.go、event_mapper.go）+ 配置/nginx。

- FunASR/Hojo 走 **xiaowei 协议**：握手 `X-Api-Key` 头鉴权
  （=funasr `--xiaowei-ws-token`）→ `session.start`
  （language/hotwords[]/input_audio_format:pcm/16000/turn_detection server_vad/
  speaker_enabled/enroll_embeddings 声纹）→ 事件流
  `speech.started → text 预览(text+stash) → speech.stopped → completed →
  session.finished`；音频 120ms 组包；收口用 `session.finish` 事件；
  客户端严格校验 `session.started` 回显
- **R2T2 是网易官方裸协议**（JSON header + EOS 字符串 + body secret_key），
  与 xiaowei 协议**完全不兼容**
- Hojo 是现成先例：xiaowei-server 里 funasr/hojo 共用同一协议实现
  （仅 providerName 不同），只需服务端实现 xiaowei 协议即可接入
- nginx 已有按路径扩展先例：`/funasr-nano/v1`→10095、`/hojo-asr/v1`→10097

**要接 R2T2 需要**：给 R2T2 服务端补 xiaowei 协议入口（仿 Hojo
`serve_streaming_ws.py` 包一层），语言钉死 Chinese；声纹（eres2netv2）和
hotwords 在 R2T2 无对应实现，需业务取舍。

## 6. 已知坑清单

1. `language` 不显式传 `"Chinese"` → 丢文本（REPORT 缺陷#1，已实测复现）
2. 客户端提前 break（把 partial 当 final）→ 结果为空，
   必须等服务端主动关连接
3. `secret_key` 服务端写死 `"test0102"`（debug 值，注释 "only for debug"），
   对外需改可配置
4. 长音频收尾曾被截断 → 已修（`max(8,...)` 补丁），当前部署含修复
5. 运行代码 `server/` 在 /tmp 时代的未提交 ws_server.py 修复已随
   迁移保留（含 .git），仓库侧改动已提交至 feat/systemd-deploy 分支

## 7. 本机 ASR 服务全景

| 服务 | 端口 | systemd 单元 | 协议 |
| --- | --- | --- | --- |
| FunASR Nano（现网） | 10095 | funasr-ws.service | xiaowei |
| HoJo ASR + 标点 | 10097 / 10098 | hojo-streaming-ws / hojo-punc | xiaowei |
| **R2T2（本次部署）** | **18272** | **r2t2-ws.service** | 裸协议 |

全部 GPU cuda:0（RTX PRO 5000 72GB，已用约 50GB）。

## 8. 可能的下一步

1. 确认 R2T2 是否进 xiaowei 链路 → 实现 xiaowei 协议入口（主要工作）
2. 声纹（现网 funasr 依赖）/热词能力评估取舍
3. `feat/systemd-deploy` 分支合并进 main（待确认）
4. secret_key 可配置化