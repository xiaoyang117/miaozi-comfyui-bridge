"""识别 API 工作流中的实际出图支路，并修改其可编辑输入。"""

import copy


SIZE_FIELDS = (
    ("width", "height"),
    ("image_width", "image_height"),
    ("latent_width", "latent_height"),
)
LINK_FIELDS = ("latent_image", "latent", "samples", "image", "pixels")


def _link(value, workflow):
    if (isinstance(value, list) and len(value) == 2
            and isinstance(value[0], str) and value[0] in workflow
            and type(value[1]) is int):
        return value[0]
    return None


def _inputs(workflow, node_id):
    node = workflow.get(node_id)
    if isinstance(node, dict) and isinstance(node.get("inputs"), dict):
        return node["inputs"]
    return {}


def _size_fields(inputs):
    for width_key, height_key in SIZE_FIELDS:
        if width_key in inputs and height_key in inputs:
            return width_key, height_key
    return None


def _is_sampler(workflow, node_id):
    node = workflow.get(node_id)
    return (isinstance(node, dict)
            and "sampler" in str(node.get("class_type", "")).lower()
            and "latent_image" in _inputs(workflow, node_id))


def _ancestors(workflow, roots):
    found = set()
    pending = list(roots)
    while pending:
        node_id = pending.pop()
        if node_id in found or node_id not in workflow:
            continue
        found.add(node_id)
        for value in _inputs(workflow, node_id).values():
            parent = _link(value, workflow)
            if parent:
                pending.append(parent)
    return found


def _dimension_target(workflow, node_id, key, seen=None):
    seen = seen or set()
    if node_id in seen:
        return None
    seen.add(node_id)
    value = _inputs(workflow, node_id).get(key)
    if type(value) is int or (isinstance(value, str) and value.isdecimal()):
        return node_id, key, int(value)
    parent = _link(value, workflow)
    if not parent or value[1] != 0:
        return None
    node = workflow[parent]
    node_type = str(node.get("class_type") or "").lower()
    if node_type == "reroute":
        for field in _inputs(workflow, parent):
            target = _dimension_target(workflow, parent, field, seen.copy())
            if target:
                return target
    if node_type in ("primitivenode", "primitiveint", "integer", "intconstant"):
        for field in ("value", "int", "integer"):
            if field in _inputs(workflow, parent):
                return _dimension_target(workflow, parent, field, seen.copy())
    return None


def _latent_size_sources(workflow, sampler_id):
    start = _link(_inputs(workflow, sampler_id).get("latent_image"), workflow)
    if not start:
        return set()
    if any(_is_sampler(workflow, node_id)
           for node_id in _ancestors(workflow, [start])):
        return set()
    pending = [start]
    seen = set()
    candidates = set()
    while pending:
        node_id = pending.pop(0)
        if node_id in seen:
            continue
        seen.add(node_id)
        if _is_sampler(workflow, node_id):
            return set()
        inputs = _inputs(workflow, node_id)
        if _size_fields(inputs):
            candidates.add(node_id)
            continue
        for field in LINK_FIELDS:
            parent = _link(inputs.get(field), workflow)
            if parent:
                pending.append(parent)
    return candidates


def describe_workflow(workflow, save_node_id=""):
    save_node_id = str(save_node_id)
    roots = ([save_node_id] if save_node_id in workflow else
             [node_id for node_id, node in workflow.items()
              if isinstance(node, dict)
              and str(node.get("class_type") or "").lower().startswith("saveimage")])
    active = _ancestors(workflow, roots)
    samplers = [node_id for node_id in workflow
                if node_id in active and _is_sampler(workflow, node_id)]
    recommended = set()
    for sampler_id in samplers:
        recommended.update(_latent_size_sources(workflow, sampler_id))
    if not recommended:
        active_sizes = [node_id for node_id in active
                        if _size_fields(_inputs(workflow, node_id))]
        if len(active_sizes) == 1:
            recommended.update(active_sizes)

    sizes = []
    loras = []
    for node_id, node in workflow.items():
        if not isinstance(node, dict) or not isinstance(node.get("inputs"), dict):
            continue
        inputs = node["inputs"]
        title = str((node.get("_meta") or {}).get("title") or node.get("class_type") or node_id)
        pair = _size_fields(inputs)
        if pair:
            width = _dimension_target(workflow, node_id, pair[0])
            height = _dimension_target(workflow, node_id, pair[1])
            sizes.append({
                "id": node_id, "title": title,
                "width": width[2] if width else None,
                "height": height[2] if height else None,
                "active": node_id in active,
                "recommended": node_id in recommended,
                "editable": bool(width and height),
            })
        if "lora_name" in inputs and "lora" in str(node.get("class_type") or "").lower():
            loras.append({
                "id": node_id, "title": title, "active": node_id in active,
                "lora_name": inputs["lora_name"],
                "strength_model": inputs.get("strength_model"),
                "strength_clip": inputs.get("strength_clip"),
                "strength": inputs.get("strength"),
            })
    targets = [{"id": node_id,
                "title": str((workflow[node_id].get("_meta") or {}).get("title")
                             or workflow[node_id].get("class_type") or node_id)}
               for node_id in samplers if "model" in _inputs(workflow, node_id)]
    return {"resolution_nodes": sizes, "lora_nodes": loras, "samplers": targets}


def apply_resolution(workflow, description, width, height, selected="auto"):
    nodes = description["resolution_nodes"]
    if selected == "auto":
        candidates = [node for node in nodes if node["recommended"]]
    else:
        candidates = [node for node in nodes if node["id"] == str(selected)
                      and node["active"]]
    if not candidates:
        raise ValueError("找不到出图支路上的分辨率节点；请选择节点或保留工作流原尺寸")
    changes = {}
    for node in candidates:
        if not node["editable"]:
            raise ValueError(f"分辨率节点 {node['id']} 的宽高由无法识别的节点计算，无法自动设置")
        inputs = _inputs(workflow, node["id"])
        width_key, height_key = _size_fields(inputs)
        for key, size in ((width_key, width), (height_key, height)):
            source = _dimension_target(workflow, node["id"], key)
            target = (source[0], source[1])
            if target in changes and changes[target] != size:
                raise ValueError("宽高共用同一个整数节点，无法设置不同的宽度和高度")
            changes[target] = size
    for (node_id, key), size in changes.items():
        workflow[node_id]["inputs"][key] = size


def apply_loras(workflow, description, overrides, extra_loras, target_id):
    available = {node["id"]: node for node in description["lora_nodes"] if node["active"]}
    for node_id, values in overrides.items():
        if node_id not in available or not isinstance(values, dict):
            raise ValueError(f"LoRA 节点不可用：{node_id}")
        inputs = workflow[node_id]["inputs"]
        for key in ("lora_name", "strength_model", "strength_clip", "strength"):
            if key in values:
                if key not in inputs or (key == "lora_name" and not str(values[key]).strip()):
                    raise ValueError(f"LoRA 节点 {node_id} 不支持 {key}")
                inputs[key] = (str(values[key]).strip() if key == "lora_name"
                               else _strength(values[key]))

    if not extra_loras:
        return
    samplers = {node["id"] for node in description["samplers"]}
    if target_id not in samplers:
        raise ValueError("请选择要添加 LoRA 的采样器")
    sampler_inputs = workflow[target_id]["inputs"]
    model_link = sampler_inputs.get("model")
    if not _link(model_link, workflow):
        raise ValueError("采样器的模型输入未连接，无法添加 LoRA")

    used = set(workflow)
    next_id = max((int(node_id) for node_id in used if node_id.isdecimal()), default=0) + 1

    def allocate():
        nonlocal next_id
        while str(next_id) in used:
            next_id += 1
        node_id = str(next_id)
        used.add(node_id)
        next_id += 1
        return node_id

    for item in extra_loras:
        if not isinstance(item, dict) or not str(item.get("lora_name") or "").strip():
            raise ValueError("LoRA 名称不能为空")
        model_only = item.get("model_only") is True
        node_inputs = {"model": model_link, "lora_name": str(item["lora_name"]).strip(),
                       "strength_model": _strength(item.get("strength_model", 1))}
        if not model_only:
            clip_inputs = {}
            pending = []
            for field in ("positive", "negative"):
                parent = _link(sampler_inputs.get(field), workflow)
                if parent:
                    pending.append(parent)
            seen = set()
            while pending:
                node_id = pending.pop()
                if node_id in seen or _is_sampler(workflow, node_id):
                    continue
                seen.add(node_id)
                inputs = _inputs(workflow, node_id)
                if ("clip" in inputs
                        and "lora" not in str(workflow[node_id].get("class_type") or "").lower()
                        and _link(inputs["clip"], workflow)):
                    clip_inputs[node_id] = inputs["clip"]
                for key, value in inputs.items():
                    parent = _link(value, workflow)
                    if key != "clip" and parent:
                        pending.append(parent)
            clip_sources = {tuple(link) for link in clip_inputs.values()}
            if len(clip_sources) != 1:
                raise ValueError("当前采样器的正负提示词没有共用同一条 CLIP 支路；请选仅模型模式")
            node_inputs["clip"] = list(next(iter(clip_sources)))
            node_inputs["strength_clip"] = _strength(item.get("strength_clip", 1))
        new_id = allocate()
        workflow[new_id] = {"class_type": "LoraLoaderModelOnly" if model_only else "LoraLoader",
                            "inputs": node_inputs}
        model_link = [new_id, 0]
        if not model_only:
            old_clip = tuple(node_inputs["clip"])
            new_clip = [new_id, 1]
            forked = {}

            def fork(link):
                parent = _link(link, workflow)
                if not parent or _is_sampler(workflow, parent):
                    return link
                if parent in forked:
                    return [forked[parent], link[1]]
                node = workflow[parent]
                changes = {}
                for key, value in _inputs(workflow, parent).items():
                    if key == "clip" and isinstance(value, list) and tuple(value) == old_clip:
                        changes[key] = new_clip
                    else:
                        updated = fork(value)
                        if updated != value:
                            changes[key] = updated
                if not changes:
                    return link
                clone_id = allocate()
                forked[parent] = clone_id
                workflow[clone_id] = copy.deepcopy(node)
                workflow[clone_id]["inputs"].update(changes)
                return [clone_id, link[1]]

            for field in ("positive", "negative"):
                sampler_inputs[field] = fork(sampler_inputs.get(field))
    sampler_inputs["model"] = model_link


def _strength(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ValueError("LoRA 权重必须是数字")
    if not -100 <= number <= 100:
        raise ValueError("LoRA 权重必须在 -100 到 100 之间")
    return number
