def _suffix(config):
    value = config.get("suffix", "")
    return value.strip().strip(",") if isinstance(value, str) else ""


def transform(hook, data, config):
    if hook == "after_prompt" and _suffix(config):
        data["prompt"] = f"{data['prompt']}, {_suffix(config)}"
    return data


def handle_api(action, payload, config):
    if action != "preview":
        return {"error": "未知操作"}
    prompt = payload.get("prompt", "")
    if not isinstance(prompt, str):
        return {"error": "请输入提示词"}
    return {"prompt": transform("after_prompt", {
        "prompt": prompt, "description": ""}, config)["prompt"]}
