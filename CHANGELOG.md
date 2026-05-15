# Changelog

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