# 喵梓二号 · Miaozi ComfyUI Bridge

LLM 驱动的 ComfyUI 文生图 / 图生图桥接 Web 应用。

用自然语言（或一张参考图）描述你想要的角色/画面，后端自动完成：
**角色识别 → 提示词生成 → 提交 ComfyUI → 返回成图**。

> 本项目是原 `comfyui-bridge` 的优化重构版，重点强化了对**本地小模型**的友好度（角色查询不再强依赖 LLM 推理）。

---

## ✨ 功能特性

- 🗣️ 自然语言 / 标签 描述生成提示词（LLM，OpenAI 兼容接口）
- 🖼️ 支持上传参考图，VLM 先识别角色外貌再生成
- 📚 **本地角色库**（24.5 万 Danbooru 角色，SQLite 离线查询，无网络依赖）
- 🔍 **多层角色解析器**（对小模型友好）：
  - 英文/罗马音直查（无需 LLM）
  - 中文别名表（`aliases.json`，内置热门角色，可增补）
  - 词级宽松匹配，LLM 输出带噪声也能命中
  - 手动指定角色标签框（完全绕过识别）
- 📐 分辨率预设下拉，自动改写工作流 `EmptyLatentImage`
- 💬 多轮对话历史、图片放大/复制/保存、聊天记录本地持久化
- 🌐 可选浏览器搜索（Playwright）与 Tavily 搜索作为角色线索补充

---

## 🚀 快速开始

### 1. 安装依赖

```bash
pip install -r requirements.txt
python -m playwright install chromium   # 仅使用浏览器搜索时需要
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
2. LLM 参考角色特征标签 + 用户描述生成正向提示词
3. 占位符替换 + 按所选分辨率改写工作流
4. 提交 ComfyUI，完成后在对话中展示图片

如果角色识别失败，可：
- 在对话栏 **"角色标签"** 框手动填 `shiroko, blue_archive` 绕过识别
- 在 `character_lookup/aliases.json` 添加中文别名（一次添加，永久生效）

---

## 📁 项目结构

```
app.py                 # Flask 后端 + SSE 生成流程
settings.py            # 配置中心（读写 settings.json）
comfyui/client.py      # ComfyUI 客户端（提交/轮询/下载/尺寸改写）
llm/                   # LLM/VLM 客户端 + 搜索
character_lookup/
  query.py             # 角色库查询（宽容匹配/英文抽取/多候选）
  resolver.py          # 多层角色解析器（规则优先，LLM 兜底）
  aliases.json         # 中文别名表（可增补）
  build_db.py          # 从 CSV 构建角色库
  characters.db        # 本地角色库（git 忽略，自行构建）
workflows/             # ComfyUI 工作流 JSON
static/ templates/     # 前端页面与样式
```

---

## ⚠️ 说明

- `settings.json`、`characters.db`、`.env` 均被 git 忽略，避免提交个人 Key 与大文件
- 工作流需为 ComfyUI 的 **API 格式**（Save API Format 导出），Canvas 格式会提示转换
- 角色库字段来自 [Laxhar/noob-wiki](https://huggingface.co/datasets/Laxhar/noob-wiki) Danbooru 角色数据集
