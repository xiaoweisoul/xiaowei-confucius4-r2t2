# R2T2 修改方案:在 ws_server.py 增加 xiaowei 协议路由接入 xiaowei-server

> **状态更新(2026-09-21):本方案已实施并验证通过**,代码见服务器
> `server/` 分支 `xiaowei-protocol`(commit f6fcd99),补丁归档
> `patches/0002-xiaowei-protocol-and-microbatch-scheduler.patch`。
> 冒烟 17/17、并发压测 4/8/16 路全达标(详见文末"实施结果")。
> 尚未完成:frpc 隧道 / nginx 路由 / xiaowei-server 切流(§3 部署侧)。

> 目标:xiaowei-server 的 `funasr_xiaowei_realtime` Provider **不改一行 Go 代码**,
> 只把 provider 配置的 ws_url 从 funasr 换到 R2T2,即可调用 R2T2。
> 本方案给出修改后的 R2T2 服务端应满足的完整协议合同(全部经代码核实,
> 关键握手校验逻辑引用 recognizer.go 行号)。

## 0. 原理:为什么不改 xiaowei-server 一行代码就能接入

xiaowei-server 的 Provider 是按**协议**实现的,不绑定具体引擎:

- `internal/asr/funasr_xiaowei_realtime` 包同时服务 FunASR(`funasr_xiaowei_realtime`)
  和 Hojo(`hojoasr_xiaowei_realtime`),两家共用同一实现,仅 providerName 不同
  (recognizer.go:80-91 `NewFunASRXiaoweiRealtimeForType`)
- Provider 建连时只做三件事(recognizer.go:153-186):拨 ws + `X-Api-Key` 头 →
  发 `session.start` → 等第一帧事件必须是 `session.started`(readHandshake,
  recognizer.go:190-239)
- Hojo 先例:`serve_streaming_ws.py` 头部注释明确写着"以 funasr_xiaowei_realtime
  provider 直连本服务可零代码切换"——这就是 R2T2 要抄的作业。

所以只要 R2T2 的 `ws_server.py` 新增一个讲 xiaowei 协议的路由,Provider 即插即用。

## 1. xiaowei-server 对服务端的调用方式(实测合同)

### 1.1 鉴权

- 建连 HTTP 头 `X-Api-Key: <token>`,服务端校验失败应 HTTP 401 拒连
  (funasr 端对应启动参数 `--xiaowei-ws-token`;R2T2 侧新增同名启动参数)

### 1.2 客户端 → 服务端帧

| 帧 | 时机 | 字段 |
| --- | --- | --- |
| `{"type":"session.start","session":{...}}` | 连接后第一帧 | `language`(可空)/`hotwords[]`(可空)/`input_audio_format`="pcm"/`sample_rate`=16000/`turn_detection`(`{"type":"server_vad",threshold,silence_duration_ms}` 或 null=manual)/`speaker_enabled`/`enroll_embeddings[]`(声纹) |
| 二进制 PCM 帧 | 持续推音频 | 16k/16bit/mono,provider 内部 120ms 组包 |
| `{"type":"session.finish"}` | 音频结束 | 要求刷新尾包后返回 session.finished |

### 1.3 服务端 → 客户端事件(type 精确值,stream.go:228-310)

| 事件 | 时机 | 必带字段 |
| --- | --- | --- |
| `session.started` | 收到 session.start 后 | `session:{id,language,input_audio_format:"pcm",sample_rate:16000,turn_detection(回显生效值),speaker_enabled:false,...}` |
| `input_audio_buffer.speech_started` | 新语音段开始 | `item_id`, `audio_start_ms` |
| `conversation.item.input_audio_transcription.text` | 预览更新 | `item_id`, `text`(稳定前缀), `stash`(可变尾), `content_index`, `language`, `audio_start_ms/audio_end_ms` |
| `input_audio_buffer.speech_stopped` | 语音段收口 | `item_id`, `audio_end_ms` |
| `conversation.item.input_audio_transcription.completed` | 段最终转写 | `item_id`, `transcript`, `content_index`, `language`, timing |
| `session.finished` | 整 session 终态 | `session:{id,reason:"client_finish"}` |
| `error` | 协议错误 | `error:{code,message,param,item_id?}` |

### 1.4 readHandshake 强校验(recognizer.go:190-239,R2T2 必须逐条满足)

1. 第一帧必须是 `session.started`(否则建连失败)
2. `session.id` 非空
3. `input_audio_format=="pcm"` 且 `sample_rate==16000`(硬编码校验)
4. **客户端 language 配置非空时,回显 language 必须与请求一致**;为空则不校验
5. **manual 模式回显 turn_detection 必须为 null;auto/realtime 模式必须回显
   `server_vad` 对象,且 threshold/silence_duration_ms 与请求完全一致**
   (即阈值参数必须真正生效,不能只回显不实现)
6. `speaker_enabled` 请求 true 而服务端回 false → 建连失败
   → **R2T2 无法支持声纹:必须在握手前直接拒绝,不能回显 true**,
   否则 R2T2 后续不产出 speaker_verify 数据,业务侧静默丢声纹语义

> 说明:readHandshake 不校验 `hotwords`——热词在 xiaowei-server 侧仅透传,
> 不做端到端验证,所以 R2T2 侧可以"接受但降级"(转成 system_prompt 或忽略+日志)。

## 1.5 (参考)funasr 服务端路由注册(xiaowei_realtime_ws.py:1906-1907)

```python
app.router.add_get("/", ws_handler)                    # 本机直连
app.router.add_get("/funasr-nano/v1", ws_handler)      # nginx 路径转发
```

nginx `location = /funasr-nano/v1` 原样转发(不剥前缀),R2T2 同理注册两条。

## 2. R2T2 侧修改方案(ws_server.py)

### 2.1 总体思路:新增路由,复用已有推理管线,不碰 v0/v1

```
@app.websocket("/xiaowei/v1")   ← 新增(nginx 用)
@app.websocket("/")             ← 新增(本机测试用),与 v0/v1 并存
```

新路由内部复用 v1 路由的现成管线(音频缓冲、160ms 切块、流式解码、
LSP 尾部补齐),把「JSON header + EOS + 增量消息」翻译成 xiaowei 事件:

```
session.start   → 初始化 asr_state(language 钉死映射,见 2.3)
PCM 二进制帧    → 喂给现有 160ms 管线(积攒满一个 chunk 才解码)
  首个非空增量   → speech.started(item_id_1, audio_start_ms=段起点)
  每个增量      → text 事件(text=累计稳定文本, stash=空, item 快照)
  VAD 段收口    → speech.stopped + completed(item_id_1, transcript=段全文)
session.finish  → 触发 finish_streaming_transcribe(与 v1 EOS 同路径)
  尾部追平      → completed(若与最后 text 相同可省略) + session.finished
  → 主动关连接
```

### 2.2 鉴权实现(Sanic middleware)

```python
@app.middleware("websocket")
async def xiaowei_ws_auth(request):
    # 只保护新路由;v0/v1 保持原样
    if request.path in ("/xiaowei/v1", "/"):
        token = request.headers.get("X-Api-Key", "")
        if token != args.xiaowei_ws_token:
            raise sanic.exceptions.Unauthorized("invalid X-Api-Key")
```

(参照 funasr 端 `ws_auth_middleware` 同款语义:缺失/不一致直接 401。)

### 2.3 语言映射(关键差异点)

R2T2 侧钉死:请求里非空 `language` 原样回显;为空时默认 `Chinese`。
**内部解码统一走显式 language(非 None)**,绕开已知缺陷 #1(language=None
时丢整轮文本)。这同时满足 readHandshake 第 4 条(非空时回显一致)。

R2T2 支持的 language 值(30 种,见 README 语言支持章节)与 Qwen3-ASR 系
命名一致(Chinese/English/Cantonese...),xiaowei-server 的 language 配置
值直接透传即可,无需映射表。

### 2.4 VAD 参数映射(满足回显强校验)

xiaowei 协议 `turn_detection` 参数 → FireRedVAD 配置换算(帧长 10ms):

| xiaowei 参数 | FireRedVAD 配置 | 默认值换算 |
| --- | --- | --- |
| threshold | `speech_threshold` | 0.5 → 0.5 |
| silence_duration_ms | `min_silence_frame = ms / 10` | 800 → 80 帧 |

- 由于 vad_config 是**进程级全局单例**(ws_server.py:266),但 threshold/
  silence 每 session 不同 → 新路由内**每会话 new 一个 StreamVadPostprocessor**
  (仅后处理器对象,模型权重共享,内存开销可忽略;`FireRedStreamVad` 类已支持
  reset,每会话 reset 并按需重建 postprocessor 即可)
- manual 模式(turn_detection=null):R2T2 天然单段语义,直接当 manual 处理
  (整连接一个 item,session.finish 收口)——与 Hojo 的 ManualTurnState 同构
- 注意 FireRedVAD 语义:`speech_threshold` 是逐帧概率阈值,与 funasr 的
  `server_vad_threshold` 语义接近但非严格等价,回显时**必须原样回显请求值**,
  实际生效的是换算后的 FireRedVAD 参数(行为差异:静音判定可能更敏感/迟钝,
  上线前用 asr-test 页实测校准)

### 2.5 热词与声纹的取舍

| 能力 | 处理 |
| --- | --- |
| hotwords[] | 接受不报错;拼进 system_prompt 通道(`resolve_qwen_context` 现成支持,ws_server.py:241)。格式建议先做纯透传(把词表 join 进 prompt),效果需实测;未验证前打日志标记 `hotwords_ignored=True` 也可接受 |
| speaker_enabled=true | **握手前拒绝**(发 `error` 事件 code=`speaker_not_supported`,然后关连接)。不能回显 true,原因见 1.4 第 6 条 |
| enroll_embeddings | 同上拒绝(或忽略,取决于业务对声纹的依赖程度,见 §5) |

### 2.6 参数化 secret_key(顺手修)

现有 `secret_key` 写死 `test0102`。新路由不需要 secret_key(用 X-Api-Key);
老路由的 secret_key 顺手改成 `--secret-key` 启动参数(默认保持 test0102 兼容)。

### 2.7 解码调度器:micro-batch(核心模块,不是可选项)

**背景(为什么必须做)**:ws_server.py 现状是 `single_process=True` +
`streaming_transcribe` 同步函数在 async 协程里直接调用(无 executor 包裹)。
单路没问题,但 xiaowei 链路是多会话并发的:asyncio 单线程事件循环里
N 路会话的解码(17~30ms/块)互相排队,6 路以上并发即逼近 160ms 块间隔,
音频积压、延迟雪崩。现网 funasr/hojo 都带 micro_batch 调度器,就是为此。

**参照实现**(直接移植,勿重写):
`/mnt/asr/funasr_nano_2512/xiaowei-funasr/examples/industrial_data_pretraining/fun_asr_nano/asr_decode_scheduler.py`
(412 行,已读过全部源码)。核心设计:

- **单 worker 线程**(`ThreadPoolExecutor(max_workers=1)`)串行调用推理引擎:
  保证同一引擎不被并发调用;asyncio 侧经 `run_in_executor` 提交,不阻塞事件循环
- **双优先级队列**:`high_jobs`(completed/final 稳定结果)优先于
  `partial_jobs`(中间预览)——段收口永远不被新来的 partial 插队
- **micro-batch 窗口**:worker 从队头取 job 后 `asyncio.sleep(micro_batch_wait_ms)`
  (默认 15ms),把窗口内到达的兼容任务合成一次批量 generate,
  **用 10~20ms 小延迟换 GPU 批量吞吐**
- **合批兼容键**:`(engine, asr_kwargs, max_new_tokens)` 完全一致才合批,
  只从队头连续合并——不同会话的语言/热词/预算不同则不合,避免互相污染
- **partial 去重**:同 partial_key 只保留最新(旧 job cancelled 并返回空文本),
  防止积压时陈旧预览排队
- **结构化日志**:decode_job_submitted/batch_started/batch_finished/job_finished
  带 queue_wait_ms/inference_ms/total_decode_ms,压测可直接解析

**R2T2 移植适配点**(与 funasr 的差异):

| funasr 原版 | R2T2 适配 |
| --- | --- |
| `vllm_engine.generate(inputs=[audio]*N)` 天然批接口 | `streaming_transcribe` 是**单路带状态**调用(asr_state 每会话独立)。合批不能直接合"解码调用",只能合"同批提交给 vLLM 的 token 生成"。**首版降级方案:不合批,仅用调度器的单 worker 线程 + 双优先级队列 + partial 去重**,即 micro_batch_size=1 模式——这已解决事件循环阻塞与排队公平性问题 |
| `_clean_asr_text` | R2T2 侧无需清洗(已有 detect_hallucination 等逻辑) |
| job_type: user_partial / completed | 映射:流式块解码=user_partial(key=requestId),finish/EOS 追平=high(completed) |

**分阶段策略**:
1. **P1(随本次修改交付)**:调度器骨架 + 单 worker + 双优先级 + partial 去重,
   `micro_batch_size=1`(无合批)——消灭事件循环阻塞,多会话时解码串行但公平,
   单路延迟回到 17~30ms 水平
2. **P2(压测后视需要)**:若并发 >8 路时排队延迟仍高,再评估 R2T2 的
   `streaming_transcribe_no_reset` 能否支持批量 token 生成(需读 r2t2_asr.py
   内部 vLLM 调用是否可多 prompt 合批);不可行则考虑多 worker 分片
   (按 requestId 哈希到 K 个单 worker,每个 worker 串行)
3. VAD 的 `detect_chunk` 同样是同步 GPU 调用,一并走调度器 executor
   (或独立 VAD worker),避免 VAD/ASR 在事件循环里互相踩

**启动参数**(与 funasr/hojo 对齐):

```python
--decode-scheduler single|micro_batch   # 默认 single(P1)
--micro-batch-wait-ms 15
--micro-batch-size 4
```

### 2.8 代码规模预估(修订)

- 新增 `xiaowei_ws_handler` 路由函数 + 事件发送 helper:~250-350 行
- **新增 `asr_decode_scheduler.py`(移植 funasr 412 行,裁剪到 ~250 行)**:
  去掉 vLLM 批量 generate 分支(P1 不合批),保留双优先级/去重/日志
- 鉴权 middleware:~10 行
- args_parser 增加 `--xiaowei-ws-token` / `--secret-key` / 调度器参数:~10 行
- 不碰 v0/v1 任何现有代码路径(仅在必要时把 v1 的解码循环抽小函数复用)
- **v0/v1 是否切到调度器**:不切。保持现状,调度器仅新路由使用
  (v0/v1 是评测/调试用途,单路场景,不引入回归风险)

## 3. 部署链路改动

```
xiaowei-server(Go)
  └─ ws_url: wss://asr.xiaoweisoul.vip/r2t2-asr/v1   ← 只改这一处配置
       └─ nginx(xiaowei1) location = /r2t2-asr/v1    ← 新增(仿 /hojo-asr/v1)
            └─ proxy_pass http://127.0.0.1:18273      ← frpc 新隧道
                 └─ xiaowei2: r2t2-ws.service --port 18273(同进程新路由)
```

R2T2 systemd 单元不变(`r2t2-ws.service` 已 enabled),只需:

1. **frpc**(xiaowei2→xiaowei1 隧道):新增 remotePort 18273 映射到本地 18272
2. **nginx**(xiaowei1):加 `location = /r2t2-asr/v1 { proxy_pass http://127.0.0.1:18273; }`
   (注意:nginx 在 xiaowei1,配置在 `/etc/nginx/sites-available/xiaoweisoul.vip.conf`
   两个 server 块都要加,共两处)
3. **xiaowei-server**:ASR provider 配置 ws_url 切到 `wss://asr.xiaoweisoul.vip/r2t2-asr/v1`
   (数据库 provider 配置或 config.yaml,视现有管理方式)

## 3.1 systemd 启动参数变化

`scripts/start_r2t2_ws.sh` 追加(参照 hojo 启动脚本 env 链模式):

```bash
# xiaowei 协议入口 token(与 funasr/hojo 的 --xiaowei-ws-token 同款)
R2T2_XIAOWEI_WS_TOKEN    # 必填,不配则新路由 401 所有连接
# 解码调度器(P1 默认 single;并发验证后可切 micro_batch)
R2T2_DECODE_SCHEDULER    # single | micro_batch
R2T2_MICRO_BATCH_WAIT_MS # 默认 15
R2T2_MICRO_BATCH_SIZE    # 默认 4
```

## 4. 冒烟与压测验证清单(实施后)

1. 本机直连 `ws://127.0.0.1:18272/xiaowei/v1`:
   - 无 X-Api-Key / 错 token → 401
   - session.start(auto)→ 收 session.started,回显校验
   - 推 test.wav → 事件序列 speech_started → text*(text=累计) → speech_stopped
     → completed → session.finish → session.finished
2. readHandshake 逐条过一遍(§1.4 六条)
3. **并发压测(必做,调度器验收)**:4/8/16/32 路并发推流,采集
   decode_job_finished 日志的 queue_wait_ms / inference_ms / total_decode_ms
   分布。验收标准:8 路并发下单路 total_decode_ms P95 ≤ 80ms
   (留 2 倍余量于 160ms 块间隔);不达标则启动 P2(§2.7)
4. 管理端 asr-test 页(已 enabled,default_target=funasr_xiaowei_realtime_ws)
   手动指定目标 URL 打完整一轮
5. **用 Provider 真跑**:xiaowei-server 切 provider 指到新路由,发语音,全链路出文本
6. 312 条对齐评测:同集同口径 CER 对比(预期与裸协议一致,推理路径未变)

## 5. 业务决策项(实施前需确认)

| 事项 | 选项 | 影响 |
| --- | --- | --- |
| 声纹 | R2T2 无实现。若现网 funasr 声纹是硬需求 → 拒绝,或业务接受缺失 | 决定 R2T2 能否切生产 |
| 热词 | system_prompt 通道透传(效果待实测) vs 忽略 | funasr 有热词文件,现网在用 |
| 标点 | R2T2 原生输出无标点?需实测确认 | hojo 有 10098 标点服务,可复用 |
| secret_key 可配置化 | 顺手修,低风险 | 安全项 |

## 6. 实施顺序(修订:调度器进 P1)

1. **asr_decode_scheduler.py 移植**(§2.7):单 worker + 双优先级 + partial 去重
   (先于路由,可独立用裸协议并发压测验证)
2. ws_server.py 新增 xiaowei 路由 + 鉴权 + 参数(核心,~300 行,解码全走调度器)
3. 本机冒烟(§4.1-4.2)+ **调度器并发压测(§4.3,用裸协议即可先测 P1 调度器)**
4. frpc 隧道 + nginx 路由(xiaowei1)
5. asr-test 页 + Provider 实跑(xiaowei-server)
6. 312 条对齐评测 + 延迟探针;若压测不达标 → P2(合批/多 worker)
7. 结果回写 REPORT.md,决策项落到业务侧

---

## 7. 实施结果(2026-09-21,已完成步骤 1-3)

### 7.1 交付物

| 文件 | 内容 |
| --- | --- |
| `server/xiaowei_ws_route.py`(新增) | xiaowei 协议路由:`/xiaowei/v1` + `/`,X-Api-Key 鉴权、session.start 强校验、text 累计快照事件、VAD 段收口(finish 追平)、speaker 请求显式拒绝、每会话独立 VAD 实例(threshold/silence 真实生效) |
| `server/asr_decode_scheduler.py`(新增) | 微批调度器 v3:单 worker 线程 + 15ms 收集窗口 + 合批 generate,partial 同 key 去重、completed 高优插队、结构化延迟日志 |
| `server/r2t2/r2t2_asr.py`(新增方法) | `batch_streaming_decode_step` + `_batch_consume_chunk`/`_batch_apply_output`:从单会话版逐行复制的输入/输出两段,中间 generate 批量化 |
| `server/ws_server.py`(修改) | 路由注册(惰性依赖注入)、鉴权、`--xiaowei_ws_token`/`--secret_key` 参数、调度器装配(`R2T2_MICRO_BATCH_WAIT_MS` 可调) |
| `patches/0002-xiaowei-protocol-and-microbatch-scheduler.patch` | 上述全部改动的 git diff 归档(应用前提:先应用 0001) |

### 7.2 调度器演进(重要教训,改并发模型前必读)

1. **v1 单 worker 串行**:安全,但 8 路并发 queue_wait P95=117ms 超标
   (每路 17ms/块 × 8 路排队是数学上限)
2. **v2 多 worker(4 线程)并行**:**vLLM V1 同步 `LLM.generate` 线程不安全**,
   并发调用触发 EngineCore 崩溃(`Assertion failed: !_current_out`,
   router.cpp:166),整个服务挂死,且会留下孤儿 EngineCore 进程占显存
   (重启前须清理,否则新实例起不来)→ 弃用
3. **v3 微批合批(定稿)**:单 worker 线程内,15ms 窗口收集多路请求,
   一次 `generate([inp1..inpN])` 批量调用——vLLM 连续批处理在引擎内部
   并行消化,线程安全且吞吐高

### 7.3 验证数据(独立端口 18280 测试实例)

**冒烟(17/17)**:401 拒连 ×2 / session.started 回显(language/format/
turn_detection 全对)/ 事件序列完整(speech_started→text→speech_stopped→
completed→session.finished)/ text 单调 / transcript 完整一致 /
speaker 拒绝 / manual 回显 null。

**并发压测(16k int16 真实节奏推流,test.wav 6.7s)**:

| 并发 | 正确率 | 墙钟 | partial total P50 | P95 | queue_wait P95 | 批推理 |
| --- | --- | --- | --- | --- | --- | --- |
| 4 路 | 4/4 | 6.79s | 41ms | 46ms | 15ms | 25ms/批(4合1) |
| 8 路 | 8/8 | 6.79s | 45ms | **51ms** | 15ms | 29ms/批(8合1) |
| 16 路 | 16/16 | 6.81s | 58ms | **65ms** | 15ms | 42ms/批(16合1) |

验收线(8 路 P95 ≤ 80ms)达标,16 路仍有余量;墙钟与单路相同,零积压。

### 7.4 实施中发现并修复的问题(坑清单)

1. `from ws_server import read_pcm` 触发模块二次加载 → Sanic app 名冲突
   崩溃;解法:工具函数复制进路由文件(上游改动需两处同步)
2. FireRedVAD postprocessor 对连续语音不一定及时置 `is_speech_start`,
   依赖它开 item 会吞掉全部 text 事件;解法:**首个稳定增量到达时开 item**
3. 段收口必须先 `finish_streaming_transcribe_no_reset` 追平(直接取
   `asr_state.text` 会丢每段 1~3 字的 LSP 尾部)
4. **xiaowei 的 `silence_duration_ms` 必须真实映射**到
   `min_silence_frame`(ms/10):进程级默认 200ms 会把自然停顿切段并丢字;
   每会话独立 VAD 实例(共享权重)同时解决并发状态污染
5. 闭包引用未定义的局部函数(`finish_decode` 定义在段收口之后)→
   NameError;worker 闭包必须在主循环前定义

### 7.5 待办(接 §3/§6)

- [ ] frpc 隧道(18273)+ nginx `/r2t2-asr/v1`(xiaowei1)
- [ ] asr-test 页 + Provider 实跑(xiaowei-server 切流前最后一步)
- [ ] 312 条对齐评测(预期与裸协议一致)
- [ ] 业务决策项(声纹/热词/标点,§5)
