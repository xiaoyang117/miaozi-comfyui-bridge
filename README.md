# 喵梓

把自己电脑上的 **ComfyUI** 和**本地模型**，用一个本机网站用起来。

- **纯本机单进程**：网站 + MCP 引擎跑在一起，不用服务器、不用桥接程序
- ComfyUI 和模型都在本机，原始画质、无额外费用
- 用 **MCP** 协议驱动 ComfyUI（失败自动回退 HTTP 直连）
- 手机、平板连同一局域网用浏览器访问也能用

---

## 它怎么工作

```
  本机浏览器（手机连同一局域网也行）
        │  http://127.0.0.1:5000
        ▼
  ┌────────────────────────────────────┐
  │  喵梓服务端（单进程）                │
  │  · 对话界面、会话历史（SQLite）      │
  │  · 调 LLM 生成提示词                │
  │  · engine.py：直接持有 MCP 连接     │
  │    - 起 comfy-mcp（stdio 子进程）   │
  │    - 驱动本机 ComfyUI 出图          │
  │    - 代理本机模型（llama.cpp 等）    │
  └────────┬──────────────┬────────────┘
           ▼              ▼
     ComfyUI (8188)   llama-server (8080)
```

没有桥接进程、没有令牌、没有长轮询——服务端进程自己就是引擎。
MCP 相关配置改完后在配置页点「重启引擎」即可重建连接。

---

## 快速开始

### 一、先在本地把两个东西跑起来

```bash
# 1. ComfyUI（如果还没启动，便携版直接 run_nvidia_gpu.bat）
python main.py --listen 127.0.0.1 --port 8188

# 2. 本机模型，默认 llama.cpp 的 llama-server（OpenAI 兼容，默认端口 8080）
llama-server -m 你的模型.gguf --port 8080
# Ollama / LM Studio 也行，在配置页把本机模型地址改掉即可
```

### 二、装 MCP 依赖

国内镜像（清华源等）目前还没有收录 comfy-mcp，要用官方源。
两个包都要装：comfy-mcp 是 MCP 服务端，comfy-cli 是它背后的引擎，
缺了后者 MCP 能握手但所有工具都会报 "comfy not found on PATH"。

```bash
pip install "comfy-mcp" "comfy-cli>=1.14.0" -i https://pypi.org/simple
```

> 如果装进了独立的虚拟环境，后面要在配置页填它们的绝对路径
> （服务端起 MCP 子进程时不会继承 venv 的 PATH）。

### 三、启动服务端

双击项目根目录的 `start.bat`，或者：

```bash
cd server
pip install -r requirements.txt
python app.py
```

打开 <http://127.0.0.1:5000>，进**配置**页：

1. 「LLM」执行位置选 `直连本机模型`，地址默认 `http://127.0.0.1:8080/v1`；
   llama-server 忽略模型名（随便填），Ollama / LM Studio 要填真实模型名
2. 「ComfyUI 与 MCP」里填：
   - **ComfyUI 工作区目录**：便携版必填，见下面的坑
   - **comfy-mcp 命令**：装在 venv 里就填绝对路径，如
     `C:/Users/you/venv/Scripts/comfy-mcp.exe`
   - **comfy 命令路径**：comfy-cli 在 venv 里才需要填，如
     `C:/Users/you/venv/Scripts/comfy.exe`
3. 工作流路径留空会自动用项目 `workflows/` 目录里第一个 json
4. 点保存。MCP 配置有改动的话，再点「本机引擎」里的**重启引擎**

**便携版用户的坑**：`comfy_project` 不填的话，comfy-cli 会用它自己的默认空目录
（`C:\Users\你\Documents\comfy\ComfyUI`），表现为模型列表是空的、
自定义节点认不到。填你的真实工作区，比如
`H:/ComfyUI_windows_portable/ComfyUI_windows_portable/ComfyUI`。

右上角徽标显示「MCP 引擎」就是全链路就绪；显示「HTTP 直连」说明 MCP 没起来
但 ComfyUI 本身连得上，出图不受影响；「离线」则要先去启动 ComfyUI。

---

## 局域网访问（可选）

手机/平板连同一个 Wi-Fi，浏览器访问 `http://<你电脑的局域网IP>:5000`。

- 防火墙放行 5000 端口（首次启动 Windows 会弹窗，允许即可）
- 建议在配置页设一个**访问口令**，避免局域网里别人乱用
- ComfyUI 需要以 `--listen 0.0.0.0` 启动才接受局域网来源的回调；
  只在本机用则保持 `127.0.0.1` 即可

---

## 配置说明

配置都在网站「配置」页里，改完点保存即可生效，不用重启；
只有 MCP 相关字段（命令、工作区、comfy 路径）要额外点「重启引擎」。

| 项目 | 说明 |
| --- | --- |
| **LLM 执行位置** | `local` = 直连本机模型（llama.cpp / Ollama / LM Studio）；`direct` = 直连远程 API |
| **本机模型地址** | local 模式用，默认 llama-server `http://127.0.0.1:8080/v1` |
| **本机模型名称** | local 模式用；llama-server 忽略，Ollama / LM Studio 填真实模型名 |
| **comfy-mcp 命令** | MCP 服务端启动命令，venv 装的填绝对路径 |
| **ComfyUI 工作区** | 便携版必填，否则 comfy-cli 用默认空目录 |
| **comfy 命令路径** | comfy-cli 在 venv 里才需要填 |
| **提示词占位符** | 工作流里那个提示词文字，默认 `114514.1919810` |
| **保存节点 ID** | 工作流里输出图片的节点 ID，填错会拿不到图 |
| **宽/高占位符** | 可选。想让网站控制分辨率才需要，见下 |
| **历史上下文轮数** | 0 = 每次全新对话。越多本地小模型越慢 |
| **保留图片数** | 自动只留最近 N 张，防止塞满磁盘 |

### 关于分辨率

网站支持按预设切分辨率，但**工作流里得先埋占位符**才不会报错。

在 ComfyUI 里把宽高节点改成占位符文字，导出 API 格式：

```json
"21": { "class_type": "EmptyLatentImage",
        "inputs": { "width": "PH_W", "height": "PH_H" } }
```

然后在配置页填：

- 宽度占位符：`PH_W`
- 高度占位符：`PH_H`

网站会把它替换成数字 `896` / `1152`（是数字，不是字符串，ComfyUI 不会报类型错）。
不填占位符也没关系，分辨率就按工作流里原本写死的值走。

---

## 常见问题

**右上角显示「离线」**
ComfyUI 没启动，或地址不对。先确认 `http://127.0.0.1:8188` 能打开。

**MCP 显示未就绪，但出图正常**
正常降级。引擎会退回直接调 ComfyUI 的 HTTP 接口，功能一样，
只是少了 MCP 的节点/模型探查能力。常见原因：
- 没装 comfy-mcp：`pip install comfy-mcp comfy-cli`
- 装在虚拟环境里但没填 `comfy_bin`，报 "comfy not found on PATH"
- 便携版没填 `comfy_project`（MCP 能握手，但模型/节点相关工具用不了）

改完这些去配置页点「重启引擎」。

**MCP 在线但模型列表是空的**
`comfy_project` 没填或填错。comfy-cli 默认用
`C:\Users\你\Documents\comfy\ComfyUI` 当工作区，你的 ComfyUI 不在那，
它自然找不到模型。把真实工作区目录填进「ComfyUI 工作区目录」即可。

**下载 / 连本地服务报 502，或提示连不上 127.0.0.1**
你开着 Clash / v2ray 之类的代理工具，环境里的 `http_proxy` 把「发给本机」的请求
也塞进代理了。引擎已经处理了（`trust_env=False` 并清掉代理变量），
包括传给 comfy-mcp 的环境也清了。如果你自己另外写了脚本，记得也这么处理。

**出图了但网站显示「没有返回图片」**
「保存节点 ID」填错了。打开工作流的 JSON，找 `SaveImage` 那个节点，
把它的 key（比如 `"9"` 或 `"66"`）填进配置。

**工作流报「不是合法 JSON」或「替换后 JSON 损坏」**
多半是手动改过工作流。正确做法：在 ComfyUI 里改完，用
**Save (API Format)** 导出，别用普通的 Save。

**提示词里带上了历史对话的内容**
「上下文」开关关掉即可，或者把「历史上下文轮数」调小。

---

## 目录结构

```
miaozi/
├── start.bat               一键启动（环境自检 + 首次装依赖 + 开浏览器）
├── server/                 服务端 + 引擎（单进程，全在本机）
│   ├── app.py              Flask 应用与 API
│   ├── engine.py           本机引擎：MCP 生命周期 + 生图 + 模型代理
│   ├── settings.py         运行时配置（data/settings.json）
│   ├── mcp_client.py       MCP stdio 客户端
│   ├── backends.py         ComfyUI(MCP/HTTP) 后端与本地模型
│   ├── store.py            会话/消息持久化
│   ├── vlm.py              图片识别
│   ├── llm/                LLM 客户端与提示词模板
│   ├── character_lookup/   本地角色库
│   ├── templates/ static/  前端
│   └── outputs/            生成的图片（自动清理）
│
├── workflows/              工作流存放处
└── _e2e/                   端到端测试脚手架
```

---

## 自检

改完代码想确认没跑坏，仓库里带了一套端到端测试（模拟 MCP 服务端 +
模拟本地模型，不需要真的 ComfyUI）：

```bash
bash _e2e/run_all.sh
```

会依次验证 MCP 握手、任务派发、占位符替换（含数字类型）、图片回传落盘、
会话上下文、异常路径、输出清理。全绿说明主链路是通的。

如果你的 ComfyUI 正在本机跑，还可以跑两个**真机**脚本（会真的出图，
约 30-60 秒一张）：

```bash
python _e2e/live_mcp_test.py   # 官方 comfy-mcp 全链路：提交→等待→取图
python _e2e/live_ph_test.py    # 宽高占位符替换成数字，出图后校验实际尺寸
```

跑之前确保 `_e2e` 脚本顶部的 VENV / PROJECT 路径指向你的环境。
这两个脚本已经在本机（RTX 5060 Ti，便携版 ComfyUI）验证通过。

## 已验证的 MCP 版本

官方 `comfy-mcp` 更新很快（合并/重命名工具不提前通知）。当前代码对齐的是
**comfy-mcp 0.10.0 + comfy-cli 1.21.0**，实测 39 个工具：

| 用途 | 工具 | 备注 |
| --- | --- | --- |
| 探测 | `server_info` / `system_stats` | 无参数 |
| 出图 | `run_workflow(workflow_path, wait, ...)` | wait=False 异步返回 prompt_id |
| 等待 | `job(action="wait", prompt_id, timeout_seconds)` | ⚠️ 旧版的 wait_for_job / watch_job / job_status 已合并成这个 |
| 取图 | `fetch_outputs(prompt_id, out_dir)` | |
| 校验 | `validate_workflow(workflow_path)` | invalid 是正常返回 valid:false，不抛异常 |

代码里对旧工具名保留了回退，老版本 comfy-mcp 也能用。
