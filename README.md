# edge-tts-web-ui

> **edge-tts 的 Web UI 版本**：一个开箱即用的微软 Edge 在线语音合成（TTS）服务，Docker 一键部署，
> 自带简体中文操作界面，并提供 **OpenAI 兼容 API**，可被 OpenAI SDK、Open WebUI、Dify 等直接调用。

## 一、项目来源与致谢

本项目站在两个优秀开源项目的肩膀上：

| 项目 | 贡献 | 说明 |
|---|---|---|
| [rany2/edge-tts](https://github.com/rany2/edge-tts) | **核心合成能力的来源** | 语音合成本体（`edge_tts.Communicate`、`list_voices`）完全由该 Python 库实现，通过微软 Edge 浏览器的在线 TTS 端点合成语音，无需任何 API Key。本项目将其包装为 HTTP 服务并容器化 |
| [DejavuMoe/edgeTTS](https://github.com/DejavuMoe/edgeTTS) | **Web 界面的参照** | 本项目的浏览器操作界面（音色选择、参数滑块、播放下载的交互布局）参考了该项目的界面设计思路，在此致谢。界面为本项目独立实现的单文件零依赖版本 |

在此之上，本项目自行实现的部分：

- **FastAPI 服务层**（`server.py`）：合成 / 音色 / 健康 / 版本 / 更新等 HTTP 接口
- **Web 操作界面**（`static/index.html`）：简体中文单文件界面，内网离线可用
- **OpenAI 兼容层**：`/v1/audio/speech` + `/v1/models`
- **容器化与更新系统**：Dockerfile、compose 编排、一键自动更新与离线上传更新

> 本目录本身克隆自上游 `rany2/edge-tts` 仓库，所有新增文件与上游代码**零重叠、零修改**，
> 可随时 `git pull` 跟进上游升级（见第九节）。

## 二、功能特性

- 🎙️ **语音合成**：微软 Edge 在线 TTS，300+ 音色（含大量中文音色），无需 API Key
- 🌐 **Web 界面**：单文件 HTML 零外部依赖，音色搜索/收藏、语速音调音量滑块、在线播放与下载
- 🔌 **OpenAI 兼容**：`POST /v1/audio/speech` + `GET /v1/models`，OpenAI 音色名自动映射中文音色
- 📖 **在线接口文档**：内置 Swagger（`/docs`）与 ReDoc（`/redoc`），全部接口中文说明
- 🔄 **自动更新**：一键从 PyPI 下载官方 whl 原地安装重启；或界面上传安装包离线更新
- 🐳 **容器化**：非 root 运行、健康检查、日志轮转、国内镜像源构建参数

## 三、快速开始

### 方式一：导入现成镜像（推荐）

```bash
# 导入镜像包（本目录产物 edge-tts-web-ui.tar 或 .tar.gz）
docker load -i edge-tts-web-ui.tar

# 用 compose 启动
docker compose up -d
```

### 方式二：现场构建

```bash
docker compose up -d --build
```

启动后：

| 入口 | 地址 |
|---|---|
| Web 操作界面 | `http://<主机IP>:9550/`（compose 默认映射 9550→容器 5050，可在 compose 里改） |
| Swagger 接口文档 | `http://<主机IP>:9550/docs` |
| 健康检查 | `http://<主机IP>:9550/health` |

> ⚠️ **运行时依赖外网**：合成调用微软在线 TTS 端点，服务器必须能出网（无需 API Key）。

## 四、构建与导出镜像（维护者用）

### Docker 环境

```bash
bash build-and-export.sh        # 构建 + 冒烟测试 + 导出 edge-tts-web-ui.tar.gz
# 或 Windows：build-and-export.bat
```

### 本机 wslc 环境（Windows，Docker Desktop 不可用时）

`wslc`（`C:\Program Files\WSL\wslc.exe`）是 WSL 容器 CLI，语法与 docker 一致，本机实测可用：

```bash
# 构建（pip 走清华源；基础镜像走国内代理源）
wslc build --build-arg PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple \
           --build-arg BASE_REGISTRY=dockerproxy.net \
           -t edge-tts-web-ui:latest .

# 运行并冒烟
wslc run -d --name edge-tts-web-ui -p 5050:5050 edge-tts-web-ui:latest
curl http://127.0.0.1:5050/health

# 导出
wslc save -o edge-tts-web-ui.tar edge-tts-web-ui:latest
gzip -k edge-tts-web-ui.tar
```

> Docker Hub 直连不通时，`BASE_REGISTRY` 可选：`docker.m.daocloud.io`（默认）/ `dockerproxy.net` / `hub.rat.dev`。

**产物**：`edge-tts-web-ui.tar`（约 312MB）、`edge-tts-web-ui.tar.gz`（约 109MB）。
镜像 tag 固定为 `latest`，升级部署无需改版本号；具体版本通过 `/health` 与界面查看。

## 五、接口一览

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/health` | 健康检查（含 edge-tts 版本与默认音色） |
| GET | `/voices?locale=zh-CN` | 音色列表，可按语言过滤 |
| GET | `/tts?text=你好&voice=...&rate=-10%` | 合成（GET 方式，返回 audio/mpeg） |
| POST | `/tts` | 合成（推荐）：`{"text":"你好","voice":"zh-CN-XiaoxiaoNeural","rate":"+0%","volume":"+0%","pitch":"+0Hz"}` |
| POST | `/v1/audio/speech` | **OpenAI 兼容 TTS**（见第六节） |
| GET | `/v1/models` | **OpenAI 兼容模型列表**（tts-1 / tts-1-hd） |
| GET | `/api/version` | 服务版本与更新功能可用状态 |
| POST | `/api/update/check` | 检查上游新版本（返回 whl 与 Release 源码包直链） |
| POST | `/api/update/run` | 一键自动更新（需 docker.sock） |
| POST | `/api/update/upload` | 离线上传安装包更新（需 docker.sock） |
| GET | `/api/update/status` | 更新任务进度（阶段/百分比/日志） |

合成示例：

```bash
curl -X POST http://192.168.1.100:5050/tts \
  -H "Content-Type: application/json" \
  -d '{"text":"三加二等于几呀","voice":"zh-CN-XiaoxiaoNeural","rate":"-10%"}' \
  -o out.mp3
```

常用中文音色：`zh-CN-XiaoxiaoNeural`（女，温柔）、`zh-CN-YunxiNeural`（男，活泼）、`zh-CN-XiaoyiNeural`、`zh-CN-YunjianNeural`。

参数说明：`rate`/`volume` 必须带显式正负号（如 `+0%`、`-20%`），`pitch` 带 `Hz` 后缀（如 `+10Hz`）；
不带符号的值会被服务端自动归一化兼容。

## 六、OpenAI 兼容调用（v1.9 起）

```bash
curl -X POST http://192.168.1.100:5050/v1/audio/speech \
  -H "Content-Type: application/json" \
  -d '{"model":"tts-1","input":"三加二等于几呀","voice":"alloy","speed":1.0}' \
  -o speech.mp3
```

| OpenAI 参数 | 本服务行为 |
|---|---|
| `model` | 兼容性参数，任意值均可（实际引擎为 edge-tts） |
| `input` | 待合成文本 |
| `voice` | OpenAI 音色名 `alloy`/`echo`/`fable`/`onyx`/`nova`/`shimmer` 自动映射中文音色；也可直接传 edge-tts 原生音色名（如 `zh-CN-YunxiNeural`） |
| `response_format` | 当前仅支持 `mp3`（edge-tts 原生输出格式） |
| `speed` | 语速倍率 0.25~4.0，换算为 edge-tts 语速百分比（1.5 → +50%） |

**可选鉴权**：compose 中设置 `OPENAI_API_KEY=你的密钥` 后，`/v1/*` 要求请求头
`Authorization: Bearer 你的密钥`（与 OpenAI 客户端填 API Key 的方式一致）；不设置则不鉴权。

**典型接入（Open WebUI）**：设置 → 音频 → TTS 引擎选 OpenAI →
Base URL 填 `http://<主机IP>:9550/v1` → API Key 任意（或填你设置的密钥）→ 模型选 `tts-1`。

## 七、Web 界面

浏览器打开 `http://<主机IP>:9550/` 即可使用，无需任何构建（界面交互参照
[DejavuMoe/edgeTTS](https://github.com/DejavuMoe/edgeTTS) 设计，独立实现）：

- **文本输入**：大文本框 + 字数统计（上限 1000）
- **音色选择**：中文音色优先排序；关键字搜索（名称/语言/性别）；☆ 收藏常用音色（localStorage 持久化）
- **参数调节**：语速 -50%~+100%、音调 ±50Hz、音量 ±50%，滑块实时显示带符号数值
- **生成与播放**：在线播放，可下载 MP3
- **系统更新面板**：版本徽章、检查更新、一键自动更新、离线上传更新、GitHub Releases 跳转
- **零外部依赖**：单文件 HTML 内联 CSS/JS，容器内网/离线环境均可打开

## 八、系统更新功能

Web 界面底部"⚙️ 系统更新"面板提供三种能力：

| 功能 | 行为 | 依赖 |
|---|---|---|
| **检查更新** | 查询 PyPI（失败回落 GitHub Releases）获取 edge-tts 最新版本并对比 | 服务器可访问外网 |
| **一键自动更新** | 检查更新 → 从 PyPI 直接下载官方 whl（约 38KB）→ `pip install` 原地装到 /app → `docker restart` 自身。**不需要 git pull 和 docker build**；仅当 whl 直装失败且配置了 SRC_DIR 时，才回退到"git pull → docker build → 重建容器"路径 | 外网 + docker.sock |
| **离线上传更新（推荐）** | 界面提供 GitHub Releases 跳转与 whl 直链 → 在自己电脑上下载 → 上传 → 服务端原地安装 → 自动重启容器。**全程无需外网、无需 git** | 仅 docker.sock |

**离线更新三步**：

1. 点更新面板的"⬇️ 下载 Release 源码包 (zip)"（Release 页的 Source code (zip) 就能用）或
   "直接下载最新 whl（PyPI，约 38KB）"，在你的电脑上下载
2. 把下载的 zip / whl 上传（也兼容 Docker 镜像包 tar.gz）
3. 服务端自动识别包类型：源码包/whl → `pip install` 到 /app → `docker restart` 生效；
   镜像包 → `docker load`（校验镜像名前缀）→ 重建容器

> 上游 GitHub Release 仅含源码压缩包（无 whl 资产）；**whl 发布在 PyPI**，直链由检查更新接口动态返回。
> 上游 tag 不带 v 前缀（如 `7.3.1`），archive 链接按真实 tag 生成。

> ⚠️ **安全提示**：挂载 docker.sock 等于把宿主机 Docker 控制权交给本容器（等价 root）。
> 请勿将端口暴露到公网，仅在家庭内网使用。
>
> ⚠️ **环境限制**：更新功能依赖真实 Docker 的 `docker.sock`（启动时校验套接字有效性）。
> wslc 环境没有 docker.sock，面板会显示引导提示并禁用按钮——请在宿主机手动升级。
>
> 📌 **compose 用户注意**：自动重建用 `docker run` 实现，compose 管理的额外设置（网络、其他卷）不会保留；
> 追求严谨时可改用"上传安装包后在宿主机 `docker load -i` + `docker compose up -d`"。

## 九、上游升级（rany2/edge-tts 发新版本时）

本目录是上游仓库的克隆，除上游 `README.md` 已被本文档替换外，其余新增文件
（`server.py`、`static/`、`Dockerfile`、`requirements.txt`、`docker-compose.yaml`、构建脚本）
与上游代码**零重叠、零修改**，升级上游不影响本项目：

```bash
cd edge-tts-web-ui
git pull                                    # 方式一：直接拉取上游最新代码
# 方式二（手动）：只覆盖 src/ setup.py setup.cfg 三个上游文件即可
#               Dockerfile 只 COPY 这三个 + README.md + 本项目自己的 server.py 和 static/
#               （README.md 现在是本文档，Dockerfile 用它生成 pip 包元数据，勿删）

wslc build --build-arg PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple \
           --build-arg BASE_REGISTRY=dockerproxy.net \
           -t edge-tts-web-ui:latest .
```

**升级时留意**：

1. 依赖的上游公开 API 只有两个：`edge_tts.Communicate` 与 `edge_tts.list_voices()`（6.x 以来稳定）
2. 上游 setup.py 新增依赖会被 Dockerfile 的源码安装自动解析，无需改 Dockerfile
3. 参数校验规则变更（如 `^[+-]\d+%$`）已被 `_normalize_param()` 吸收，一般无需再动
4. 冒烟必跑 4 条：`/health`（看版本号）、`/`、`/tts`、`/voices`
5. 若上游将来添加同名文件导致 `git pull` 冲突，保留本项目版本即可——服务层实现完全独立
6. 上游 `README.md` 已被本文档替换（git 视为本地修改）：若 `git pull` 提示 README.md 冲突，
   始终保留本文档版本（`git checkout --ours` 或先 `git stash` 再恢复），本文档即项目主文档

## 十、注意事项

1. **运行时依赖外网**：合成调用微软在线 TTS 端点，首次延迟约 0.5~2 秒，建议上层做文本缓存
2. **并发与限流**：微软端点对高频调用可能返回无音频，建议客户端重试或按文本缓存结果
   （锁屏答题类场景尤其适合：题目表达式有限，可预合成缓存）
3. **已内置的保护**：文本长度上限（默认 1000）、非 root 用户（UID 10001）、健康检查、日志轮转（10MB×3）
4. **对接 kidslock 等客户端**：Android 侧可用 `MediaPlayer` 直接播 HTTP 响应；
   锁屏场景务必设计超时降级——网络不通时退回本机预录音，避免孩子因网络问题解不开锁

## 十一、更新记录

| 版本 | 变更 |
|---|---|
| 1.0 | 首个可用版本：FastAPI 服务层 + Docker 镜像 |
| 1.1 | 新增 Web 操作界面 `static/index.html`（音色搜索/收藏、参数滑块、播放下载），界面参照 DejavuMoe/edgeTTS 设计 |
| 1.2 | **修复 502 Bug**：edge-tts 要求参数带显式正负号，`0%` 会被拒；服务端 `_normalize_param()` 统一归一化，前端同步发带符号值，旧客户端兼容 |
| 1.3 | 新增界面更新功能：版本检查 / 一键自动更新 / 手动更新入口；镜像内新增 git 与 docker CLI |
| 1.4 | 新增在线接口文档（`/docs`、`/redoc` 入口，全部接口中文摘要）；**镜像 tag 统一为 `latest`**，部署无需改版本号 |
| 1.5 | 离线更新重构：改为"界面上传安装包 → 服务端安装 → 重启"模式 |
| 1.6 | 离线更新对接上游 Release：支持官方 whl/sdist 原地 pip 安装；检查更新返回 whl 直链 |
| 1.7 | 修正并支持 Release 源码包：tag 无 v 前缀的 archive 直链修正；上传支持 Source code (zip/tar.gz)，源码离线构建用 `--no-build-isolation` |
| 1.8 | **自动更新流程轻量化**：改为"PyPI 下载 whl → 原地安装 → docker restart"，git 路径仅作回退；**镜像更名为 `edge-tts-web-ui`** |
| 1.9 | **新增 OpenAI 兼容接口**：`/v1/audio/speech` + `/v1/models`，OpenAI 音色自动映射中文音色，可选 `OPENAI_API_KEY` Bearer 鉴权 |

## 许可

- 本项目服务层与界面代码遵循上游项目同源的开源精神自由使用
- 合成能力来自 [rany2/edge-tts](https://github.com/rany2/edge-tts)，请遵循其 LICENSE（GPL-3.0）
- 界面设计参照 [DejavuMoe/edgeTTS](https://github.com/DejavuMoe/edgeTTS)，感谢原作者
- 语音合成服务由微软 Edge 在线 TTS 端点提供，请合理使用，勿高频滥用
