# 开发本地插件

插件是**本机执行的 Python 代码**，只安装自己信任的插件。运行时与主服务共享
Python 进程，没有沙盒；启用后插件代码可以访问这台电脑上服务进程能访问的资源。
如果要从手机或局域网管理插件，请先在配置页设置访问口令。
网页不提供远程上传、在线市场或自动安装第三方依赖。

## 安装与管理

1. 把一个插件文件夹复制到 `server/data/plugins/`，如
   `server/data/plugins/prompt_suffix/`；每个文件夹必须有 `plugin.json` 和
   `main.py` 两个普通文件。可将 `examples/plugins/prompt_suffix/` 复制过去体验。
2. 打开网站「插件」页，点「重新扫描并载入代码」，查看版本、扩展点或报错。
3. 勾选「启用插件」，按需填写 JSON 配置和执行顺序，点击保存。
   启用状态和配置写入 `server/data/plugins.json`，无需重启网站即可生效。
4. 要更新插件，先停用，替换其文件，再点「重新扫描并载入代码」和启用；
   卸载时停用并删除该插件文件夹。启动和 ZIP 更新保留 `server/data/`。

插件代码由当前 Python 运行环境加载。插件要使用额外第三方包时，开发者需
自己安装到启动网站所用的 Python 环境；插件管理器不会运行 `pip`。
如果加载或调用时报错，网站会继续完成生成；报错显示在插件页和服务日志里。
出错插件会暂停执行，修好文件后点击「重新扫描并载入代码」重新加载。

## 文件格式

`plugin.json` 示例：

```json
{
  "api_version": 1,
  "id": "prompt_suffix",
  "name": "提示词追加示例",
  "version": "1.0.0",
  "description": "在新创作的提示词后追加内容",
  "hooks": ["after_prompt"],
  "requires": [],
  "api": true,
  "ui": "panel.js",
  "default_config": {"suffix": "cinematic lighting"}
}
```

- `api_version` 必须是整数 `1`；不兼容版本会显示错误且不会执行。
- `id` 与目录名一致，只能使用小写字母、数字、横线、下划线，开头必须是字母。
- `name`、`version` 必填；`description` 可选。
- `hooks` 必填，不能重复。只提供接口或面板的插件可填空数组。
  插件只会收到声明过的扩展点。
- `requires` 可选，是其他插件 ID 的列表。依赖插件需先启用，执行时会先运行依赖插件；
  停用依赖插件之前需停用使用它的插件。执行顺序数字越小越早，同值按 ID 排序。
- `default_config` 可选，是新装插件的初始 JSON 配置；用户修改后保留在运行数据目录。
- `api` 为可选布尔值，开启后可使用同一访问口令保护的插件 API。
- `ui` 可选，是 `assets/` 中**单个 JS 文件名**，启用插件后在插件页显示
  「打开插件面板」。该 JS 会在网站页面内执行，只放置可信的资源。

`main.py` 需提供函数：

```python
def transform(hook: str, data: dict, config: dict) -> dict:
    return data
```

插件内的其他 Python 文件可通过相对导入使用，例如 `from .helpers import helper`。
修改入口或辅助文件后需重新扫描；需要释放资源的后台任务应由插件自己管理。

可选的生命周期函数：`on_enable(config: dict)` 在启用或重载后首次加载时执行，
`on_disable()` 在停用或重载前执行。依赖插件先启用、后停用。
不要在这些函数中启动无法结束的后台线程；网站退出时不保证执行 `on_disable()`。

`data`、`config` 是各插件收到的独立副本。应返回 `data` 字典，且保留该扩展点
要求的字段及类型。一个插件的异常或无效返回会保留进入它之前的数据，不会
回滚此前已成功的插件。插件以指定顺序运行，别直接改磁盘上的原始工作流。

| 扩展点 | 何时执行 | 可修改的必要字段 |
| --- | --- | --- |
| `before_prompt` | 新创作，在调用 LLM 前；不用于再次生成 | `user_input: str`、`history_text: str`、`character_info: str`、`specific: bool`、`custom_system: str` |
| `after_prompt` | 新创作，在常驻正向内容拼接后、ComfyUI 出图前；不用于再次生成 | `prompt: str`（非空）、`description: str` |
| `before_workflow` | 新创作及再次生成，在尺寸、LoRA、二采和随机种子处理后、提交 ComfyUI 前 | `workflow: dict`（API 格式工作流） |
| `after_generate` | 新创作及再次生成，在引擎返回图片后、图片保存和历史写入前 | `image: str`（非空的 data URL 或图片 URL）、`description: str` |

`before_prompt` 的 `data` 还包括 `workflow_path`。
`after_prompt` 的 `data` 还包括 `workflow_path`。
`before_workflow` 的 `data` 还包括 `prompt`、`width`、`height` 和
`is_regeneration`。`after_generate` 的 `data` 还包括 `prompt`、
`workflow_path`、`is_regeneration`。这些信息只供参考，
该阶段仅把表中注明的可修改字段传回主流程。

`after_generate` 可以替换图片或说明，但图片应继续使用受支持的 data URL /
图片 URL 格式；为了避免损坏历史，插件应保留原图并只在必要时修改。
本功能没有为插件代码提供系统权限隔离或运行超时机制。

## 插件 API 与页面

若清单配置 `"api": true`，可在 `main.py` 中增加：

```python
def handle_api(action: str, payload: dict, config: dict) -> dict:
    return {"received": action}
```

网站向 `POST /api/plugins/<id>/call/<action>` 转发 JSON 对象，要求返回
可编码为 JSON 的字典。停用插件后无法调用；异常同样显示在插件页。
插件资源可放在 `assets/` 目录，通过
`GET /api/plugins/<id>/assets/<filename>` 读取；只接受单个普通文件名，
不会返回 `main.py` 或 `plugin.json`。这两个接口都受网站现有访问口令保护。

`ui` 文件可以注册 `window.miaoziPluginPanels[id] = (container, api, config) => { ... }`，
其中 `container` 是当前插件面板，`api` 是带访问口令的网页请求函数，
`config` 是当前配置。界面中展示外部文字时使用 `textContent`，不要拼到
`innerHTML`。示例插件同时展示了生成钩子、配置、插件 API 和插件面板。
