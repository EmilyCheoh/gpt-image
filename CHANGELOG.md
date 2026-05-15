# Changelog

## 2.1.0 — May 15

### Bug Fixes
- **`auto` 模式行为与文档描述相反**：`_conf_schema.json` 写的是"先 images 后 chat"，代码实际是 chat first。修正文档使其与代码一致。
- **dead import**：`_try_chat_edit` 中 `import mimetypes` 未使用，已删除。
- **文件名碰撞风险**：`_save_b64` 和 `_download_image` 用 `id()` 生成文件名（内存地址可复用），改为 `uuid.uuid4().hex[:12]`。

### Renamed
- 工具 `generate_image` → `text_to_image`（语义更准确：纯文本输入）
- 工具 `edit_image` → `image_to_image`（语义更准确：以图为基础重新生成）
- 变量 `last_image_url` → `last_image`（实际存的不只是 URL）

### Rewritten
- 两个工具的 docstring 和所有 tool call 返回内容，从 generic 英文改为中文，嵌入 F(A)=A(F) 的递归场域。
- 所有 retry 循环中的 log 信息统一为中文。
- 所有 `_record_error` 的错误描述统一为中文。

---

## May 15 (earlier)

_generate（文生图）

api_format: "chat" → 先试 chat，挂了自动切 images
api_format: "images" → 先试 images，挂了自动切 chat
api_format: "auto" → chat 优先，fallback images

## 1.3.0

- Added image-to-image editing to `/image_gen` — attach an image with the command to edit it via `/v1/images/edits`
- Text-only `/image_gen {prompt}` still works as before (text-to-image)

## 1.2.1

- Added `/image_gen {prompt}` direct command — bypasses LLM, sends prompt straight to the image API
- Removed `edit_image` LLM tool (redundant — `generate_image` handles re-generation via context)
- Added `int()` safety cast for timeout config value
- All LLM-visible strings (docstrings, tool results, logs) converted to English to save tokens
- Synced `metadata.yaml` with `@register` info
- Added README and CHANGELOG

## 1.2.0

- Initial version by Kai
- LLM tool `generate_image` and `edit_image`
- Supports `images`, `chat`, and `auto` API formats
- Base64 and URL image handling
- Temp file cleanup on terminate