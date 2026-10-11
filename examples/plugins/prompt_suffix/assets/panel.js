window.miaoziPluginPanels = window.miaoziPluginPanels || {};
window.miaoziPluginPanels.prompt_suffix = (panel, api, config) => {
  const label = document.createElement('label');
  label.textContent = '试写一段提示词，查看插件如何追加内容';
  const input = document.createElement('input');
  input.placeholder = 'a cat';
  const button = document.createElement('button');
  button.className = 'btn-secondary';
  button.textContent = '预览';
  const result = document.createElement('p');
  result.className = 'hint';
  button.onclick = async () => {
    try {
      const response = await api('/api/plugins/prompt_suffix/call/preview', {
        method: 'POST',
        body: {prompt: input.value},
      });
      const data = await response.json();
      result.textContent = data.prompt || data.error || '预览失败';
    } catch (error) { result.textContent = error.message; }
  };
  panel.append(label, input, button, result);
};
