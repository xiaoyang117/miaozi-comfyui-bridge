# 插件制作教程

这份教程从一个“给提示词追加内容”的插件开始，逐步说明配置、工作流修改、
插件接口和网页面板。只需要 Python 基础；只有制作网页面板时才需要 JavaScript。
已有的完整示例在 [`examples/plugins/prompt_suffix/`](../examples/plugins/prompt_suffix/)。

插件是本机执行的可信 Python 代码，与网站共享进程，没有沙盒，
启用后拥有服务进程的权限。局域网管理时请先在配置页设置访问口令。
网页负责启停和配置，安装、更新和卸载通过本机文件夹完成；
不会自动下载插件或安装第三方依赖。

## 目录

- [一、做出第一个插件](#一做出第一个插件)
- [二、清单字段怎么填](#二清单字段怎么填)
- [三、配置怎么读取和保存](#三配置怎么读取和保存)
- [四、选择合适的生成扩展点](#四选择合适的生成扩展点)
- [五、示例：修改工作流采样步数](#五示例修改工作流采样步数)
- [六、增加插件接口和网页面板](#六增加插件接口和网页面板)
- [七、依赖、执行顺序与生命周期](#七依赖执行顺序与生命周期)
- [八、调试和常见问题](#八调试和常见问题)
- [九、发布、更新和卸载](#九发布更新和卸载)

## 一、做出第一个插件

目标：新创作时在提示词末尾追加 `cinematic lighting`，并允许在网页修改它。

### 1. 建立文件夹

在项目根目录下建立以下结构；没有 `plugins` 文件夹就自己创建：

```text
server/
└── data/
    └── plugins/
        └── prompt_suffix/
            ├── plugin.json
            └── main.py
```

Windows 建议打开“显示文件扩展名”，避免文件实际变成 `main.py.txt`。
两个文件保存为 UTF-8；`plugin.json` 不要带 BOM。
也可以直接把项目中的 `examples/plugins/prompt_suffix/` 文件夹复制到
`server/data/plugins/`，它已经包含后面介绍的接口和网页面板。

### 2. 写清单

在 `plugin.json` 中填写：

```json
{
  "api_version": 1,
  "id": "prompt_suffix",
  "name": "提示词追加",
  "version": "1.0.0",
  "description": "在新创作的正向提示词末尾追加指定内容",
  "hooks": ["after_prompt"],
  "default_config": {
    "suffix": "cinematic lighting"
  }
}
```

JSON 使用英文双引号，不能写注释，最后一个字段后面不能有逗号。
`id` 必须与文件夹名称完全相同。

### 3. 写入口

在 `main.py` 中填写：

```python
def _suffix(config):
    value = config.get("suffix", "")
    if not isinstance(value, str):
        raise ValueError("suffix 必须是字符串")
    return value.strip().strip(",").strip()


def transform(hook, data, config):
    if hook == "after_prompt" and _suffix(config):
        data["prompt"] = f"{data['prompt']}, {_suffix(config)}"
    return data


if __name__ == "__main__":
    result = transform(
        "after_prompt",
        {"prompt": "a cat", "description": "一只猫"},
        {"suffix": "warm light"},
    )
    assert result["prompt"] == "a cat, warm light"
    assert result["description"] == "一只猫"
    assert transform(
        "after_prompt",
        {"prompt": "a cat", "description": ""},
        {"suffix": ""},
    )["prompt"] == "a cat"
    print("插件自测通过")
```

- `hook`：当前执行到哪个扩展点。
- `data`：该阶段的输入。这里的 `prompt` 是即将交给 ComfyUI 的完整提示词。
- `config`：网页保存的插件配置。
- `return data`：把结果交回主流程。即使不修改，也必须返回字典。

### 4. 安装、启用和试用

1. 在项目根目录打开终端，运行下面的自测。它不连接 LLM 或 ComfyUI：

   ```powershell
   python server/data/plugins/prompt_suffix/main.py
   ```

   按本节手写入口时，成功会输出 `插件自测通过`。
   直接复制仓库示例时没有这段自测，可用第六节的预览面板检查。
   请使用启动网站的同一个 Python 环境。
2. 启动网站，打开「插件」页，点击「重新扫描并载入代码」。
3. 找到「提示词追加」，勾选「启用插件」，点击「保存插件设置」。
4. 在创作页新生成一张图片，展开英文提示词，末尾应有 `cinematic lighting`。
   这一步需要实际 LLM 和 ComfyUI；前面的自测只能验证插件函数。
5. 将插件配置改成下面的内容并保存，下一次新创作末尾应变为 `warm light`：

   ```json
   {"suffix": "warm light"}
   ```

「再次生成」复用已保存的完整提示词，不执行 `after_prompt`，因此不会重复追加。
只改插件配置后点击旧图片的「再次生成」，仍会使用旧图片保存的后缀；
要使用新后缀，请发起新创作。项目已有的「常驻正向内容」在此插件之前拼接，
不要在两处填写相同内容，以免重复。

## 二、清单字段怎么填

| 字段 | 必填 | 规则 |
| --- | --- | --- |
| `api_version` | 是 | 当前只能是整数 `1`，不能写成字符串 `"1"` |
| `id` | 是 | 与文件夹名一致；1～64 个字符，以小写字母开头，后面只能有小写字母、数字、`-`、`_` |
| `name` | 是 | 网页显示的名称，非空字符串 |
| `version` | 是 | 非空字符串，建议使用 `1.0.0` 这样的版本号 |
| `description` | 否 | 插件介绍，字符串 |
| `hooks` | 是 | 扩展点列表，不可重复；只提供 API 或面板时可用 `[]` |
| `default_config` | 否 | 初始配置，JSON 对象，默认 `{}` |
| `requires` | 否 | 依赖的其他插件 ID 列表，默认 `[]`；不可重复、不可包含自身 |
| `api` | 否 | 是否开放插件接口，布尔值，默认 `false` |
| `ui` | 否 | `assets/` 下的面板脚本文件名，如 `panel.js`；默认没有面板 |

插件文件夹、`plugin.json` 和 `main.py` 必须是普通目录或文件，不能使用符号链接。
`ui` 和 API 的动作名称只能使用英文大小写字母、数字、`-`、`_`、`.`，
且不能以 `.` 开头；不能包含路径分隔符。

即使 `hooks` 是空数组，也必须提供 `transform(hook, data, config)`；
可以直接 `return data`。只声明需要的扩展点，不必把四个都填上。

## 三、配置怎么读取和保存

网页「插件配置（JSON 对象）」保存的是该插件自己的配置。
启用状态、执行顺序和配置持久化到 `server/data/plugins.json`；
不要放进网站的全局配置，也不要把用户配置写回 `plugin.json`。

`default_config` 只在没有保存过配置时使用。
已保存配置不会自动与新版 `default_config` 合并，升级插件新增字段时应给出默认值，
例如教程中的 `config.get("suffix", "")`。修改清单里的默认值，不会覆盖用户已有配置。

运行时传给插件的 `data` 和 `config` 都是独立副本：

- 修改 `data` 并返回，可以影响当前生成流程。
- 修改 `config` 不会自动保存，下次调用仍会读取网页保存的配置。
- 配置保存后，后续 `transform` 和 `handle_api` 会收到新配置。
- 网页面板拿到的是打开时的配置；保存配置后重新打开面板可取得新值。

执行顺序必须是 `-1000`～`1000` 的整数。配置必须是可保存的 JSON 对象，
序列化后的长度不能超过 65536 个字符。
不要在配置中保存大图片、模型数据或密钥；插件没有独立的密钥管理功能。

## 四、选择合适的生成扩展点

新创作的调用顺序：

```text
角色检索、参考图识别和历史准备
  → before_prompt
  → LLM 生成提示词和说明
  → 拼接常驻正向内容
  → after_prompt
  → 替换工作流占位符，处理尺寸、LoRA、二采、种子
  → before_workflow
  → ComfyUI 出图
  → after_generate
  → 保存图片和历史
```

再次生成的调用顺序：

```text
读取历史中的完整提示词和说明
  → 处理工作流参数并更换种子
  → before_workflow
  → ComfyUI 出图
  → after_generate
  → 保存新的图片和历史
```

每个插件必须返回字典，保留该扩展点的全部必要字段和类型。
最简单的写法是直接修改传入的 `data` 并返回它，避免遗漏字段。
额外参考字段可以读取，但修改它们不会直接改变主流程对应的设置。

### `before_prompt`：调整送给 LLM 的输入

角色检索、参考图识别和历史准备完成后调用；修改 `user_input` 不会重新触发角色检索。

| 必要字段 | 类型 | 含义 |
| --- | --- | --- |
| `user_input` | 非空字符串 | 当前用户描述，可能已包含参考图识别文字 |
| `history_text` | 字符串 | 本轮带入的历史，可能为空 |
| `character_info` | 字符串 | 已检索的角色特征，可能为空 |
| `specific` | 布尔值 | 是否启用精细还原 |
| `custom_system` | 字符串 | 自定义系统指令，可能为空 |

参考字段：`workflow_path`。适合修改模型输入或指令，不用于修改采样器。

### `after_prompt`：调整完整正向提示词和说明

| 必要字段 | 类型 | 含义 |
| --- | --- | --- |
| `prompt` | 非空字符串 | LLM 输出与常驻正向内容组合后的提示词 |
| `description` | 字符串 | 中文画面说明，允许为空 |

参考字段：`workflow_path`。适合追加风格词、清理标签或修改说明。
这里修改后的提示词会用于出图，并在成功后保存到历史。

### `before_workflow`：调整提交给 ComfyUI 的工作流

必要字段：`workflow`，非空字典，必须是 ComfyUI **API 格式**，不是网页编辑器的
`nodes` / `links` 导出格式。每个节点的键为字符串，节点必须包含
字符串 `class_type` 和字典 `inputs`；整个工作流必须能编码为 JSON，不能有 NaN 或 Infinity。
通过这项检查不代表 ComfyUI 已确认节点存在或连接正确。

参考字段：`prompt`、`width`、`height`、`is_regeneration`。
宽高是当前任务传入的值，若选择沿用工作流尺寸，应以工作流节点中的值为准。
修改 `width` / `height` 本身不会再次执行尺寸适配，需要修改 `workflow` 中对应节点。
插件改动也不会重新计算已经取得的基础尺寸元数据。

内置尺寸、LoRA、二次采样和种子处理已经完成，因此插件对同一节点的修改会覆盖它们。
只修改收到的工作流副本，别改磁盘上的原始工作流文件。

### `after_generate`：处理图片和说明

只有引擎返回了图片才执行；出图失败时不会调用。

| 必要字段 | 类型 | 含义 |
| --- | --- | --- |
| `image` | 非空字符串 | 图片 data URL 或 HTTP(S) 图片地址 |
| `description` | 字符串 | 将要写入历史的中文说明 |

参考字段：`prompt`、`workflow_path`、`is_regeneration`。
修改 `prompt` 不会重新出图或改写已确定的历史提示词。

替换图片时只能返回 HTTP(S) 地址或 `data:image/` 开头的 data URL；
后者必须以 `;base64` 结束头部，并在逗号后包含有效的 base64 数据。
格式检查不检查实际图片内容或远程地址的可访问性，需要插件自行验证。
修改后的说明会与新结果一起保存；保留 `image` 不变即可只处理说明。

## 五、示例：修改工作流采样步数

这个插件把所有具有数值型 `steps` 的标准 `KSampler` 节点改为指定步数；
它会作用于主采样和二次采样。若只想改其中一个，应让用户配置目标节点，
不能靠固定节点 ID 猜测。它不处理 `KSamplerAdvanced` 或自定义采样器。

建立 `server/data/plugins/sampler_steps/`，其中的 `plugin.json` 为：

```json
{
  "api_version": 1,
  "id": "sampler_steps",
  "name": "采样步数覆盖",
  "version": "1.0.0",
  "description": "修改标准 KSampler 的数值型采样步数",
  "hooks": ["before_workflow"],
  "default_config": {
    "steps": 24
  }
}
```

`main.py` 为：

```python
def transform(hook, data, config):
    if hook != "before_workflow":
        return data
    steps = config.get("steps", 24)
    if type(steps) is not int or steps < 1:
        raise ValueError("steps 必须是正整数")
    for node in data["workflow"].values():
        if (
            node["class_type"] == "KSampler"
            and type(node["inputs"].get("steps")) in (int, float)
        ):
            node["inputs"]["steps"] = steps
    return data
```

如果 `steps` 是连接到其他节点的数组，示例会保留连接。
要让插件只作用于新创作，可以在 `before_workflow` 分支内检查
`data["is_regeneration"]` 并在为 `True` 时直接返回。

## 六、增加插件接口和网页面板

继续扩展第一节的 `prompt_suffix`，做一个不需要 LLM 和 ComfyUI 的后缀预览按钮。
完整结构：

```text
prompt_suffix/
├── plugin.json
├── main.py
└── assets/
    └── panel.js
```

### 1. 开启接口和面板

把第一节的 `plugin.json` 改为：

```json
{
  "api_version": 1,
  "id": "prompt_suffix",
  "name": "提示词追加",
  "version": "1.1.0",
  "description": "追加提示词并提供预览面板",
  "hooks": ["after_prompt"],
  "api": true,
  "ui": "panel.js",
  "default_config": {
    "suffix": "cinematic lighting"
  }
}
```

### 2. 增加 Python 接口

在 `main.py` 中保留原有函数，在 `if __name__ == "__main__":` 之前添加：

```python
def handle_api(action, payload, config):
    if action != "preview":
        return {"error": "未知操作"}
    prompt = payload.get("prompt", "")
    if not isinstance(prompt, str) or not prompt.strip():
        return {"error": "请输入非空提示词"}
    result = transform(
        "after_prompt",
        {"prompt": prompt, "description": ""},
        config,
    )
    return {"prompt": result["prompt"]}
```

请求 `POST /api/plugins/prompt_suffix/call/preview` 时，网站会把动作名称 `preview`、
请求的 JSON 对象和当前配置传给 `handle_api`。
返回值必须是能编码为 JSON 的字典，服务直接返回它，不额外包装 `success`。

预期输入和输出：

```json
{"prompt": "a cat"}
```

```json
{"prompt": "a cat, cinematic lighting"}
```

上面的输出以当前配置为 `cinematic lighting` 为前提。
插件返回 `{"error": "..."}` 仍是 HTTP 200，需要前端自行识别；
抛出异常则由插件管理器记录错误、返回 HTTP 400，并暂停插件调用。
可预期的用户输入错误建议直接返回错误对象。

### 3. 编写网页面板

在 `assets/panel.js` 中填写：

```javascript
window.miaoziPluginPanels = window.miaoziPluginPanels || {};
window.miaoziPluginPanels.prompt_suffix = (container, api, config) => {
  const hint = document.createElement('p');
  hint.textContent = `当前追加内容：${config.suffix || '留空'}`;
  const input = document.createElement('input');
  input.placeholder = 'a cat';
  const button = document.createElement('button');
  button.type = 'button';
  button.className = 'btn-secondary';
  button.textContent = '预览';
  const result = document.createElement('p');
  button.onclick = async () => {
    button.disabled = true;
    try {
      const response = await api('/api/plugins/prompt_suffix/call/preview', {
        method: 'POST',
        body: {prompt: input.value},
      });
      const data = await response.json();
      if (!response.ok || data.error) {
        throw new Error(data.error || '预览失败');
      }
      result.textContent = data.prompt;
    } catch (error) {
      result.textContent = error.message;
    } finally {
      button.disabled = false;
    }
  };
  container.append(hint, input, button, result);
};
```

注册名称 `prompt_suffix` 必须与清单 ID 一致。更换插件 ID 时，也要修改注册名和请求地址。

- `container`：系统为当前面板创建的 DOM 容器，把控件加在这里。
- `api`：已有的网页请求函数，自动携带访问口令，`body` 可直接传对象；
  返回标准 `Response`，需要自己调用 `.json()` 并检查 `.ok`。
- `config`：打开面板时的配置对象；修改它不会持久化。

停用插件，保存，再重新扫描、启用并保存。点击「打开插件面板」，输入 `a cat`，
点「预览」即可检查效果，无需真实出图。
面板脚本在每次打开时执行，应把事件绑定到自己的控件，避免重复注册全局事件。
外部文字使用 `textContent`，不要直接拼进 `innerHTML`。

### 4. 系统接口参考

| 方法与地址 | 作用 |
| --- | --- |
| `GET /api/plugins` | 列出插件、启用状态、配置和错误 |
| `PUT /api/plugins/<id>` | 保存插件的 `enabled`、`order`、`config` |
| `POST /api/plugins/reload` | 重新扫描并重新载入插件代码 |
| `POST /api/plugins/<id>/call/<action>` | 调用插件的 `handle_api` |
| `GET /api/plugins/<id>/assets/<filename>` | 读取已启用插件的资源 |

所有接口继承网站的访问口令保护。
外部请求设置 `X-Access-Password` 请求头；JSON 请求使用
`Content-Type: application/json`。网页面板使用传入的 `api` 即可。
不要把访问口令写进插件源代码或资源 URL。

保存接口只接受下面三个字段，可以只提交其中一部分，其余沿用当前值：

```json
{
  "enabled": true,
  "order": 0,
  "config": {"suffix": "warm light"}
}
```

`config` 整体替换已保存配置，不是逐个字段合并。
管理接口成功时返回 `{"success": true, "plugins": [...]}`；
配置或调用错误通常是 HTTP 400，资源不存在是 HTTP 404，
口令验证失败是 HTTP 401。

静态资源只支持 `assets/` 下的单个普通文件名，不支持子目录或符号链接。
通过传入的 `api` 获取受保护资源；普通图片标签或 CSS 链接不能自动携带这个请求头。

## 七、依赖、执行顺序与生命周期

### 依赖与执行顺序

清单中可加入 `"requires": ["prompt_suffix"]`，表示当前插件依赖 `prompt_suffix`：

- 依赖插件必须先安装并启用，系统不会自动安装或打开它。
- 每次执行某个扩展点时，声明了该扩展点的依赖插件先运行；
  依赖优先于数字顺序。依赖没有声明该扩展点时，只提供加载和可用性依赖。
- 无依赖时，执行顺序数字小的先运行，同值按插件 ID 排序。
- 停用被依赖的插件前，先停用使用它的插件。
- 缺失、出错或循环依赖会显示诊断，相关插件不会继续参与生成。

不要依赖“另一插件偷偷加了一个字段”而不声明依赖；公开并约定双方需要的数据格式。

### 生命周期

入口可额外提供以下两个函数：

- `on_enable(config)`：插件激活时执行，可初始化必要资源。
- `on_disable()`：停用或重新扫描前执行，可释放资源。

依赖先激活，停用或重新扫描时按相反顺序释放。
网站启动后，已启用插件在首次被插件管理器加载时激活，
不一定恰好在 Flask 启动时执行。
**只保存配置不会再次调用已经激活插件的 `on_enable`**；
如果初始化资源依赖配置，需要停用后重新启用，或重新扫描。

网站退出不保证执行 `on_disable()`，不要把重要持久化操作只放在里面。
插件可以通过相对导入引用同目录辅助模块，如 `from .helpers import helper`。
修改入口或辅助文件后都需要重新扫描；修改版本号本身不会自动重载代码。

## 八、调试和常见问题

每次修改代码后，先在网页停用并保存，再修改文件、重新扫描、启用并保存。
重新扫描会重载全部插件，不只是当前一个；请在没有生成任务时操作。

| 问题 | 检查方法 |
| --- | --- |
| 网页看不到插件 | 检查是否多套了一层文件夹、是否用了大写或非法目录名、是否点击重新扫描 |
| ID 不匹配 | 文件夹名称与 `plugin.json` 的 `id` 必须相同 |
| 清单不是有效 JSON | 检查英文引号、尾逗号、注释、UTF-8 BOM、文件是否实际叫 `.json.txt` |
| API 版本不兼容 | 使用整数 `1`；升级时参考当前主程序支持的版本 |
| 缺少入口函数 | 所有插件都需要 `transform`；`api: true` 的插件还需要 `handle_api` |
| 返回的数据格式不正确 | 返回字典，保留本阶段必要字段和类型；不要返回 `None` 或空提示词 |
| 保存配置后没有变化 | 先点保存；初始化资源需要重新激活；再次生成沿用旧提示词，面板需重新打开 |
| 工作流参数没变化 | 清单声明 `before_workflow`；检查真实节点类型和输入是否为连接数组 |
| 打不开面板 | 检查 `ui` 文件、注册名称和浏览器控制台；插件须已启用且没有错误 |
| 缺少 Python 包 | 安装到启动网站的同一 Python 环境，别只装进 ComfyUI 的环境 |

插件异常和无效返回会显示在插件卡片，并输出到服务端日志。
运行中的异常会丢弃该插件这次对数据的修改，保留进入它之前的值；
此前成功插件的结果不会回滚，其余可用插件仍会继续。
出错插件暂停后续执行，修复代码后重新扫描；重新保存设置也会尝试清除错误，
但不会重新读取已经加载的 Python 代码。

这是异常与数据隔离，不是权限或超时隔离。
插件函数在当前进程内串行调用，耗时函数或无限循环会阻塞调用；
请避免在扩展点中下载大模型、长时间等待外部服务或启动无法结束的线程。
网络请求应自行设置超时。

若插件有额外依赖，在自己的说明中列出已验证版本，
使用启动网站的 Python 和[清华 PyPI 镜像](https://mirrors.tuna.tsinghua.edu.cn/help/pypi/)安装；
插件管理器不会执行 `pip`。本教程两个 Python 示例只使用标准功能，不需要额外依赖。

维护主程序时，可在项目根目录用 PowerShell 运行已有插件测试：

```powershell
$env:PYTHONPATH = "server"
python -m unittest discover -s server -p "test_plugins.py" -q
```

这套测试覆盖主程序的插件运行时和随项目提供的示例，
不会自动测试你新写的插件；自己的插件还应覆盖配置为空、非法配置、
正常生成、再次生成和停用后的行为。语法检查或预览通过也不等于真实出图已验证。

## 九、发布、更新和卸载

### 发布给别人使用

发出包含 `plugin.json`、`main.py` 和可选 `assets/`、辅助模块的插件文件夹；
可以打包成 ZIP，但用户需要自己解压后复制，网页不接收 ZIP 上传。
不要让用户直接把多一层外包装的文件夹放进插件目录。

在插件自己的说明中写清：

1. 插件 ID、版本、所需主程序 API 版本。
2. 扩展点、会修改哪些内容、是否影响再次生成。
3. 配置字段、默认值和有效范围。
4. Python 依赖和经过验证的版本；依赖的其他插件。
5. 安装、验证、更新、停用方法，以及已知不支持的节点或工作流。

不要打包用户的 `server/data/plugins.json`、生成图片、访问口令或模型 API Key。
先测试无插件状态、单独启用插件和启用多个插件的情况。

### 更新

1. 在网页停用并保存；依赖者先停用。
2. 备份插件文件夹和现有配置，替换插件文件，保持 ID 不变。
3. 点「重新扫描并载入代码」。
4. 检查新版是否新增配置字段，按说明填写；默认配置不会覆盖旧值。
5. 先启用依赖，再启用当前插件，保存后验证效果。

### 卸载

先停用并保存，再删除 `server/data/plugins/<id>/` 文件夹，最后重新扫描。
已保存配置仍保留；以后安装同一 ID 的插件会沿用之前保存的配置。
主程序更新脚本保留 `server/data/` 中的插件代码和配置，
不会自动替你升级第三方插件。
