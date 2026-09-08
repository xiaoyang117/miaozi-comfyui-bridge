# 喵梓二号 · Miaozi ComfyUI Bridge

LLM 驱动的 ComfyUI 文生图 / 图生图桥接 Web 应用。

用自然语言（或一张参考图）描述你想要的角色/画面，后端自动完成：
**角色识别 → 用户确认 → 提示词生成 → 提交 ComfyUI → 返回成图**。

> 专为 **Anima 系底模**（如 miaomiaoHarem_anima14 / animaBasev10Txt）设计：
> 提示词采用「自然语言 + 关键标签」的混合风格，兼顾模型对标签的敏感与自然语言的表达能力。
> 本项目是原 `comfyui-bridge` 的优化重构版，重点强化了对**本地小模型**的友好度（角色查询不再强依赖 LLM 推理）。

---

## ✨ 功能特性

- 🗣️ **混合模式提示词生成**：LLM 直接输出「自然语言为主 + danbooru 标签点缀」的描述（适配 Anima/Qwen 系底模），多角色自动分句、特征不串
- 👤 **无角色不虚构**：未识别到角色时强制使用通用描述（如 "a young woman"），不会编造 `Aiko (Original Character)` 之类的假角色
- 📚 **本地角色库**（24.5 万 Danbooru 角色，SQLite 离线查询，无网络依赖）
- 🔍 **多层角色解析器**（对小模型友好）：
  - 英文/罗马音直查（无需 LLM）
  - 中文别名表 + 中文名大表（`aliases.json` / `zh_names.json`，可增补导入）
  - 中文多角色同框识别（"长门和拉菲" → 两个角色）
  - 场景词 / 动作词自动过滤，不会被误判成角色
- 🎭 **角色确认弹窗**：只要识别到角色就让用户确认——多角色逐个选、同名跨作品消歧、**可选「无」剔除不想要的角色**（彻底杜绝 LLM 自己加戏）
- 🖼️ 支持上传参考图，VLM 先识别角色外貌再生成
- 📐 分辨率预设下拉，自动改写工作流 `EmptyLatentImage`
- 💬 **结构化多轮上下文**：history 以结构化消息传给 LLM，历史设定（角色/服装等）跨轮延续；上下文用量进度条实时显示
- 🔄 **再次生成按钮**：用上一条提示词原样重roll（`raw_prompt` 直通，不经过 LLM、不入上下文），seed 自动随机保证每次出图不同
- 🖼️ 图片放大 / 复制 / 保存 / 二采（img2img 放大重绘）
- 📊 任务面板：网页/API 触发的生成均实时可见进度

---

## 🚀 快速开始

### 1. 安装依赖

```bash
pip install -r requirements.txt
```

Windows 可直接双击 `start.bat`（自动装依赖 + 启动）。

### 2. 构建本地角色库（可选但推荐）

`characters.db` 体积较大（约 103MB，未随仓库分发），首次使用请运行：

```bash
# 从 HuggingFace 下载数据并构建（约 37MB CSV）
python character_lookup/build_db.py
# 若需代理: python character_lookup/build_db.py --proxy http://127.0.0.1:7897
```

### 3. 配置并启动

```bash
# 复制配置模板并填写
cp settings.example.json settings.json   # Windows: copy settings.example.json settings.json
python app.py
```

打开 http://127.0.0.1:5000 ，在 **配置页** 填写：
- **LLM 地址/Key/模型**（提示词生成，必填）
- **VLM 地址/Key/模型**（图生图识别，可选）
- **ComfyUI 地址**（默认 `http://127.0.0.1:8188`）
- 选择工作流、默认分辨率、保存节点 ID

> 无配置文件时程序会自动生成默认 `settings.json`。

---

## 🎨 使用示例

对话栏输入：

```
画一个 碧蓝档案 的白子 穿泳装
```

处理链路：
1. 解析角色 → 别名表命中 `shiroko`（本地库直查，无需 LLM）
2. 弹窗让你确认角色（多角色可逐个选 / 选"无"剔除）
3. LLM 参考角色特征 + 用户描述，输出「自然语言 + 标签」混合提示词
4. 占位符替换 + 按所选分辨率改写工作流
5. 提交 ComfyUI，完成后在对话中展示图片

**多角色同框**：输入「碧蓝航线的长门和拉菲，两人站在沙滩上」→ 识别出两个角色，
弹窗逐个确认后，LLM 会分句描述每个人（外貌/服装/动作），特征不串。

如果角色识别失败，可：
- 在对话栏 **"角色标签"** 框手动填 `shiroko, blue_archive` 绕过识别
- 在 `character_lookup/aliases.json` 添加中文别名（一次添加，永久生效）

### 📥 批量导入中文名（开源数据集，可选）

内置导入器 `character_lookup/import_zh_names.py`，可把现成的
「Danbooru 标签 → 中文名」对照表一键并入本地查询（**不依赖 LLM 翻译**）：

```bash
python character_lookup/import_zh_names.py <数据集文件或目录>
# <文件> 支持 .sqlite/.db/.csv；给目录则自动遍历其中全部数据文件
```

- 现成数据集：HuggingFace `Aligadai/danbooru-10w-zh_cn`
  （https://huggingface.co/datasets/Aligadai/danbooru-10w-zh_cn，两列 CSV）；
  或任何含 `category` 列（4=角色/3=作品）的翻译表（如已下线的
  ffdkj/ffdkj-Danbooru_Tag-Chinese-English-Translation-Table 结构）
- **质量三重保险**：只保留本库真实存在的角色（与 characters.db join）、
  丢弃通用标签/画师类目、清洗解释性后缀 —— 机翻/垃圾条目进不来
- 产出 `character_lookup/zh_names.json`（中文名→角色）与 `zh_works.json`（作品名），
  不覆盖手工 `aliases.json`；运行时自动命中，重启即生效
- 角色库重建：`python character_lookup/build_db.py`

---

## 🔌 OpenAI 兼容接口

服务内置 **OpenAI Images API 兼容层**，任何标准 OpenAI 客户端（Python / JS / curl 等）
只需把 `base_url` 指向本服务即可直接调用生图。

```python
from openai import OpenAI

client = OpenAI(
    base_url="http://127.0.0.1:5000/v1",  # 指向本服务
    api_key="miaozi",                     # 未配置 MIAOZI_API_KEY 时可任意填
)

# 生图（url 模式）
resp = client.images.generate(
    model="dall-e-3",                     # 任意 model 均接受
    prompt="1girl, shiroko, blue archive, swimsuit",
    size="1024x1024",                     # 或 "auto" 交给 AI 决定
    response_format="url",                # 或 "b64_json"
)
print(resp.data[0].url)

# 启用智能链路（本地角色库识别 + LLM 生成提示词）：传 role 即自动走智能链路
resp2 = client.images.generate(
    model="miaozi-image-xl",
    prompt="碧蓝档案的白子 穿泳装",        # 中文描述也可
    role="白子",                          # 手动指定角色（可选）
)
```

端点一览：

| 端点 | 说明 |
|---|---|
| `GET  /v1/models` | 模型列表（miaozi-image-xl / dall-e-3 兼容别名） |
| `POST /v1/images/generations` | 文生图。参数 `prompt / model / size / n / response_format` 均按 OpenAI 规范 |

扩展参数（非标准，OpenAI 客户端忽略、脚本可用）：`role`（指定角色）、`use_search`（角色库搜索）、`workflow_path`、`image`（参考图 base64 列表）、`raw_prompt`（直通模式，见下）。

**直通模式（`raw_prompt: true`，默认开启）**：prompt 视为标准英文标签/提示词
**原样直通 ComfyUI**，跳过 LLM 与角色搜索（不依赖 LLM 服务，秒级出图）。
仅当传入 `role` / `use_search=true` / `raw_prompt=false` 时才走智能链路。
直通模式下 `size: "auto"` 不会调用 LLM，直接采用默认尺寸。

可选鉴权：设置环境变量 `MIAOZI_API_KEY` 后，客户端必须带 `Authorization: Bearer <key>`。

---

## 📁 项目结构

```
app.py                 # Flask 后端 + SSE 生成流程 + OpenAI 兼容层注册
openai_api.py          # OpenAI 兼容接口层（/v1/images/generations 等）
settings.py            # 配置中心（读写 settings.json）
comfyui/client.py      # ComfyUI 客户端（提交/轮询/下载/尺寸改写/seed随机化）
llm/client.py          # LLM/VLM 客户端 + 混合模式提示词常量
character_lookup/
  query.py             # 角色库查询（宽容匹配/英文抽取/多候选）
  resolver.py          # 多层角色解析器（中文多角色/消歧/噪音过滤）
  tag_vocab.py         # Danbooru 标签词库召回与校验（tag_full.sqlite）
  aliases.json         # 中文别名表（可增补）
  build_db.py          # 从 CSV 构建角色库
  characters.db        # 本地角色库（git 忽略，自行构建）
workflows/             # ComfyUI 工作流 JSON（API 格式）
static/ templates/     # 前端页面与样式
```

---

## ⚠️ 说明

- `settings.json`、`characters.db`、`.env`、`tag_full.sqlite` 等均被 git 忽略，避免提交个人 Key 与大文件
- 工作流需为 ComfyUI 的 **API 格式**（Save API Format 导出），Canvas 格式会提示转换
- 底模适配：工作流默认用 Anima 系底模（`animaBasev10Txt` + `miaomiaoHarem_anima14`），
  提示词风格自动按工作流名匹配（含 `anima/qwen/miao` 转空格标签格式）
- 角色库字段来自 [Laxhar/noob-wiki](https://huggingface.co/datasets/Laxhar/noob-wiki) Danbooru 角色数据集
- 标签词库来自 ffdkj + qdlabs 中英对照合并（`character_lookup/merge_tags.py` 构建）

---

## 🧰 依赖服务

| 服务 | 地址 | 说明 |
|---|---|---|
| ComfyUI | `http://127.0.0.1:8188` | 生图引擎（需加载对应底模/工作流） |
| LLM（OpenAI 兼容） | `http://127.0.0.1:8080/v1` | 提示词生成（本地 qwen 等小模型即可） |
