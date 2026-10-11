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

生成结果的图片操作栏有「再次生成」：直接复用已保存的完整提示词，
跳过 LLM、角色检索和参考图识别，不重复添加常驻正向内容。
新结果保存在同一会话，沿用原分辨率、工作流和 LoRA 参数；
旧历史没有记录工作流时使用当前选择的工作流。
再次生成会更换工作流中数值型的 `seed` / `noise_seed`，以生成新的图片。

每次生成时，LLM 用同一次请求返回英文绘图提示词和 1～2 句中文「画面说明」。
说明展示在图片上方并随结果保存到会话历史；英文提示词可展开查看或复制，
仅提示词会交给 ComfyUI，说明不会写入工作流。
「再次生成」复用原说明，不再调用 LLM；旧历史或仅返回纯文本提示词的模型仍能出图，
但没有说明。说明是生成前的画面安排，不是对实际出图的识图结果。

模型输出格式为 `{"prompt": "英文正向提示词", "description": "中文画面说明"}`，
该要求也会追加到自定义系统提示词后。代码块包裹的 JSON 可正常读取；
JSON 损坏或字段类型错误会提示重试，避免把整段结构化内容当成绘图提示词。

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

依赖安装使用[清华 PyPI 镜像](https://mirrors.tuna.tsinghua.edu.cn/help/pypi/)。
两个包都要装：comfy-mcp 是 MCP 服务端，comfy-cli 是它背后的引擎，
缺了后者 MCP 能握手但所有工具都会报 "comfy not found on PATH"。

```bash
python -m pip install "comfy-mcp" "comfy-cli>=1.14.0" -i https://pypi.tuna.tsinghua.edu.cn/simple
```

> 如果装进了独立的虚拟环境，后面要在配置页填它们的绝对路径
> （服务端起 MCP 子进程时不会继承 venv 的 PATH）。

### 三、启动服务端

双击项目根目录的 `start.bat`，首次安装依赖时会使用清华源；或者手动运行：

```bash
cd server
python -m pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
python app.py
```

### 更新源码（Windows）

先关闭运行网站的命令行窗口，再双击项目根目录的 **`update.bat`**。
提示输入代理地址时，可填本机代理的 **HTTP 端口**，如 `http://127.0.0.1:7890`；
不要填 SOCKS 端口。留空会使用系统代理（若未配置则直连）。
代理只用于这次更新，不会改动系统或 Git 的全局设置。更新完重新运行 `start.bat`。
如果 GitHub 下载较慢，也可在命令行运行 `python update.py --proxy http://127.0.0.1:7890`。

- 用 Git 克隆的项目：仅在 `main` 分支且没有已修改的项目文件时快进拉取
  `origin/main`；否则停止并提示处理本地改动。
- 下载 ZIP 解压的项目：从 GitHub 下载 `main` 分支的 ZIP，仅更新同名项目文件，
  不删除额外文件。被替换的旧文件保存在项目中的 `.miaozi-update-backups` 文件夹，
  需要恢复本地修改时可从中取回。
- 两种方式都保留配置、会话、已生成图片、角色数据库和自己的额外工作流；
  `server/data/plugins/` 中安装的插件及其启用设置也会保留；
  若修改了项目自带的工作流文件，ZIP 更新前的版本会在上述备份中。
  下载或解压失败不会覆盖现有文件。

打开 <http://127.0.0.1:5000>，进**配置**页：

1. 「LLM」填写 API 地址、API Key 和模型名称。本机 llama-server 地址通常是
   `http://127.0.0.1:8080/v1`，API Key 可留空、模型名可填 `local-model`；
   Ollama / LM Studio 填各自的地址和真实模型名。远程服务填其 OpenAI 兼容 API 地址、Key 和模型名
2. 「ComfyUI 与 MCP」里填：
   - **ComfyUI 工作区目录**：便携版必填，见下面的坑
   - **comfy-mcp 命令**：装在 venv 里就填绝对路径，如
     `C:/Users/you/venv/Scripts/comfy-mcp.exe`
   - **comfy 命令路径**：comfy-cli 在 venv 里才需要填，如
     `C:/Users/you/venv/Scripts/comfy.exe`
3. 工作流路径留空会自动用项目 `workflows/` 目录里第一个 json
4. 点保存。MCP 配置有改动的话，再点「本机引擎」里的**重启引擎**

### 插件（可选）

把可信插件的文件夹复制到 `server/data/plugins/`，在网站「插件」页重新扫描、
启用并配置。插件可参与 LLM 提示词、ComfyUI 工作流、出图结果，
还可提供独立的 API 与网页面板；没有插件时原流程照常工作。
插件默认关闭，卸载或更新前先停用。完整格式和可运行示例见
[插件开发说明](docs/plugins.md) 和 `examples/plugins/prompt_suffix/`。

### 角色特征库（可选）

默认读取 `server/character_lookup/characters.db`。这个数据库不随源码分发；
如果已有建好的 `characters.db`，直接放在该位置即可。如果只有
`danbooru_character.csv`，放到同目录，在项目根目录运行：

```bash
python server/character_lookup/build_db.py --skip-download
```

没有 CSV 时，运行 `python server/character_lookup/build_db.py` 会下载数据并建库。
如要将数据库放在别处，可在启动服务前设置环境变量 `CHARACTER_DB` 为数据库的绝对路径。
配置页会显示角色库是否就绪。关闭「启用角色库检索」后生成时不会查询角色库；
遇到同名的不同作品角色，会提示选择，取消则直接生成提示词。
描述多位已知角色时，分别写清角色名和作品名（例如「白子（蔚蓝档案）和初音未来合影」）。
系统最多分别匹配四位角色；同名角色会逐个提示选择作品，未命中的角色仍可按原描述生成。
多角色续写时用「她们／他们」可沿用上一轮的全部角色；只说「她／他」时请写明角色名，以免认错人。

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
| **LLM API 地址** | 本机 llama-server 常用 `http://127.0.0.1:8080/v1`；远程服务填写其 OpenAI 兼容接口地址 |
| **LLM API Key** | 本机模型通常可留空；远程服务按需填写 |
| **LLM 模型名称** | llama-server 可填 `local-model`；Ollama / LM Studio 及远程服务填实际模型名 |
| **comfy-mcp 命令** | MCP 服务端启动命令，venv 装的填绝对路径 |
| **ComfyUI 工作区** | 便携版必填，否则 comfy-cli 用默认空目录 |
| **comfy 命令路径** | comfy-cli 在 venv 里才需要填 |
| **提示词占位符** | 工作流里那个提示词文字，默认 `114514.1919810` |
| **常驻正向内容** | 每次加在生成提示词前，可填质量、风格等词；留空则不添加。工作流中仍需保留提示词占位符 |
| **保存节点 ID** | 工作流里输出图片的节点 ID，填错会拿不到图 |
| **宽/高占位符** | 旧工作流或复杂尺寸节点可继续使用，见下 |
| **历史上下文轮数** | 0 = 每次全新对话。越多本地小模型越慢 |
| **保留图片数** | 自动只留最近 N 张，防止塞满磁盘 |

### 关于分辨率

创作页选择分辨率后，程序会沿保存图片节点和采样器的连接，
只修改实际出图支路中的尺寸输入（如 `EmptyLatentImage`、`EmptySD3LatentImage`）。
示例工作流有两个尺寸节点，但只有接入采样器的节点会被自动修改。
展开「工作流尺寸与 LoRA」可查看识别结果，也能手动指定出图支路上的尺寸节点。
关联的整数节点和 Reroute 可以跟踪；复杂计算或宽高共用一个整数节点时会明确报错，
不会静默写错尺寸。选「保持工作流原尺寸」则不自动改宽高。

原有的宽高占位符仍可使用：在 ComfyUI 里设置占位符并导出 API 格式：

```json
"21": { "class_type": "EmptyLatentImage",
        "inputs": { "width": "PH_W", "height": "PH_H" } }
```

然后在配置页填：

- 宽度占位符：`PH_W`
- 高度占位符：`PH_H`

网站会把它替换成数字 `896` / `1152`（是数字，不是字符串，ComfyUI 不会报类型错）。
选择预设时，占位符先替换，再按出图支路识别尺寸；复杂节点无法自动控制时，
仍可使用占位符。如果工作流使用了宽高占位符，生成时请选分辨率预设。

### 关于 LoRA

把 LoRA 模型放入 ComfyUI 的 `models/loras` 目录（子目录也可），重启或刷新 ComfyUI。
在创作页展开「工作流尺寸与 LoRA」：

- 工作流已有 `LoraLoader` / `LoraLoaderModelOnly` 时，可以分别修改文件名和已有的权重。
- 工作流没有 LoRA 节点时，点「添加 LoRA」，填写模型文件名、模型/文本权重；
  可以添加多条按顺序串联。默认接在选定采样器的模型与正负提示词的 CLIP 支路；
  仅需调整模型或工作流没有共用 CLIP 支路时，选「仅模型」。
- ComfyUI 在线时会列出已安装的模型名，离线时可手动填写准确的文件名。
- 点「保存此工作流参数」，切换工作流时会分别记住设置；
  也可以不保存，直接用于本轮生成。再次生成会复用原图的参数。

工作流文件不会被改写。选择了不存在的 LoRA 文件或本机缺少对应节点时，
ComfyUI 会返回错误；如果已有 LoRA 节点没有接入出图支路，不会应用对它的修改。

### 二次采样专区

创作页展开「二次采样专区」，默认选择「保持工作流原设置」，不会自动增加采样。

- **调整已有二采**：沿 latent、图像放大和 VAE 编码连接识别第二轮及后续采样，
  可调整步数、CFG、降噪、采样器与调度器。`KSamplerAdvanced` 显示开始/结束步数、
  加噪和剩余噪声选项；没有降噪输入的节点不会额外加入该字段。
  支持 `LatentUpscale`、`LatentUpscaleBy`、`ImageScale`、`ImageScaleBy`
  的已有尺寸、倍数、方法和裁剪参数。由上游节点计算的参数保持连接，可在 ComfyUI 中调整。
- **新增二采**：单轮 `KSampler` 直接接入 `VAEDecode` / `VAEDecodeTiled` 时可选，
  在最终解码前串入 `LatentUpscaleBy` 和第二个 `KSampler`，继承初采的模型、LoRA、
  正负提示词及种子。默认 1.5 倍、20 步、0.35 降噪，可按需要修改。
  已有二采或连接方式无法安全识别时，请调整已有节点或先在 ComfyUI 中连接。
- 参数按工作流分别保存，也可只用于本轮；再次生成会复用原图的二采参数并更换种子。

采样器与调度器请填写当前 ComfyUI 支持的名称。降噪越高，画面变化通常越大；
提高放大倍数会增加显存占用与耗时。「保持工作流原设置」保留工作流本来就有的二采，
不会删除已有节点。修改和新增节点只影响本轮提交，原始工作流文件不变。

---

## 常见问题

**右上角显示「离线」**
ComfyUI 没启动，或地址不对。先确认 `http://127.0.0.1:8188` 能打开。

**MCP 显示未就绪，但出图正常**
正常降级。引擎会退回直接调 ComfyUI 的 HTTP 接口，功能一样，
只是少了 MCP 的节点/模型探查能力。常见原因：
- 没装 comfy-mcp：`python -m pip install comfy-mcp comfy-cli -i https://pypi.tuna.tsinghua.edu.cn/simple`
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
