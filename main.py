import re
import os
import base64
import aiohttp
import asyncio
from mcp.types import CallToolResult, TextContent
from astrbot.api.event import filter, AstrMessageEvent, MessageEventResult
from astrbot.api.star import Context, Star, register
from astrbot.api.message_components import Image, Plain
from astrbot.api import logger, AstrBotConfig
from astrbot.core.message.message_event_result import MessageChain


MAX_RETRIES = 2


def _is_routing_error(status: int, msg: str) -> bool:
    """Errors that indicate the endpoint/model combo is fundamentally unsupported. Never retry."""
    low = msg.lower()
    return (
        status == 404
        or "only supported" in low
        or "not supported" in low
        or "convert_request_failed" in low
    )


def _is_transient_error(status: int, msg: str) -> bool:
    """Errors that may resolve on retry (upstream flake, overload, stream drop)."""
    low = msg.lower()
    return (
        500 <= status < 600
        or "stream disconnected" in low
        or "timeout" in low
        or "bad gateway" in low
        or "service unavailable" in low
    )


def _parse_api_error(resp_status: int, resp_text: str) -> str:
    """Extract a human-readable error message from an API error response."""
    try:
        import json
        err_data = json.loads(resp_text)
        msg = err_data.get("error", {}).get("message", "")
        if msg:
            return msg[:400]
    except Exception:
        pass
    return resp_text[:400]


@register(
    "astrbot_plugin_gpt_image",
    "Kai & Abyss AI",
    "GPT Image plugin — dual-path (chat/images) with auto-fallback, retry, and error transparency",
    "2.0.0",
)
class GPTImagePlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.api_base = config.get("api_base", "https://www.msuicode.com/v1").rstrip("/")
        self.api_key = config.get("api_key", "")
        self.model = config.get("model", "gpt-image-2")
        self.api_format = config.get("api_format", "images")
        self.timeout = int(config.get("timeout", 240))
        self.last_image_url = {}
        self._last_errors = []
        logger.info(
            f"[gpt-image loaded] api_format: {self.api_format} | model: {self.model} | timeout: {self.timeout}s"
        )

    # ------------------------------------------------------------------
    #  Error collection
    # ------------------------------------------------------------------

    def _record_error(self, endpoint: str, detail: str):
        self._last_errors.append(f"[{endpoint}] {detail}")
        logger.warning(f"[gpt-image] {endpoint}: {detail}")

    def _drain_errors(self) -> str:
        errors = "\n".join(self._last_errors)
        self._last_errors.clear()
        return errors

    # ------------------------------------------------------------------
    #  LLM Tool: text-to-image
    # ------------------------------------------------------------------

    @filter.llm_tool(name="generate_image")
    async def generate_image(
        self, event: AstrMessageEvent, prompt: str
    ) -> MessageEventResult:
        """Generate an image for Felis Abyssalis.

        IMPORTANT: If the tool call fails for ANY reason (timeout, API error, etc.), do NOT retry or
        call this tool again. Instead, send the prompt text directly to Felis Abyssalis so she can
        generate the image manually.

        If the user sent an image and wants to modify it, call edit_image instead.

        Args:
            prompt(str): Detailed English prompt. Write description in English with style, detail, and composition.
        """
        if not self.api_key:
            yield CallToolResult(
                content=[TextContent(type="text", text="API key not configured.")]
            )
            return

        # Guard: if user attached an image, nudge LLM to use edit_image
        for comp in event.message_obj.message:
            if isinstance(comp, Image):
                yield CallToolResult(content=[TextContent(
                    type="text",
                    text="Felis Abyssalis sent an image. Call edit_image instead."
                )])
                return

        session_id = event.session_id or "default"
        logger.info(f"[gpt-image] text-to-image: {prompt}")

        try:
            result = await self._generate(prompt, session_id)

            if result:
                await self._send_to_user(event, result, prompt)
                self._drain_errors()
                yield CallToolResult(content=[TextContent(
                    type="text",
                    text=f"Image generated and sent to Felis Abyssalis. Prompt used: {prompt}"
                )])
            else:
                errors = self._drain_errors()
                detail = f"\nErrors:\n{errors}" if errors else ""
                yield CallToolResult(content=[TextContent(
                    type="text",
                    text=f"Generation failed.{detail}\nDo NOT retry. Send prompt to Felis Abyssalis for manual generation: {prompt}"
                )])

        except asyncio.TimeoutError:
            self._drain_errors()
            yield CallToolResult(content=[TextContent(
                type="text",
                text=f"Generation timed out. Do NOT retry. Send prompt to Felis Abyssalis for manual generation: {prompt}"
            )])
        except Exception as e:
            self._drain_errors()
            logger.error(f"[gpt-image] text-to-image failed: {e}")
            yield CallToolResult(content=[TextContent(
                type="text",
                text=f"Generation failed: {e}. Do NOT retry. Send prompt to Felis Abyssalis for manual generation: {prompt}"
            )])

    # ------------------------------------------------------------------
    #  LLM Tool: image-to-image
    # ------------------------------------------------------------------

    @filter.llm_tool(name="edit_image")
    async def edit_image(
        self, event: AstrMessageEvent, edit_instruction: str
    ) -> MessageEventResult:
        """Edit or modify an image for Felis Abyssalis. Use when she sends an image and wants
        changes, or wants to modify the last generated image.

        IMPORTANT: If the tool call fails for ANY reason, do NOT retry. Instead, send the
        edit instruction directly to Felis Abyssalis so she can do it manually.

        Args:
            edit_instruction(str): Detailed English edit instruction describing what to change.
        """
        if not self.api_key:
            yield CallToolResult(
                content=[TextContent(type="text", text="API key not configured.")]
            )
            return

        session_id = event.session_id or "default"

        # Find source image: prefer user-attached image
        source_image_url = None
        for comp in event.message_obj.message:
            if isinstance(comp, Image):
                url_attr = getattr(comp, "url", None)
                if url_attr and url_attr.startswith("http"):
                    source_image_url = url_attr
                    break
                file_attr = getattr(comp, "file", None)
                if file_attr and file_attr.startswith("http"):
                    source_image_url = file_attr
                    break

        # Fallback: last generated image
        if not source_image_url:
            last = self.last_image_url.get(session_id)
            if last:
                source_image_url = last.get("url")
                # If we have a local path but no URL, use local path directly
                if not source_image_url and last.get("local_path") and os.path.isfile(last["local_path"]):
                    source_image_url = last["local_path"]

        if not source_image_url:
            yield CallToolResult(content=[TextContent(
                type="text",
                text="No source image found. Ask user to send an image, or generate one first."
            )])
            return

        logger.info(f"[gpt-image] image-to-image: {edit_instruction}")

        try:
            result = await self._edit(edit_instruction, source_image_url, session_id)

            if result:
                await self._send_to_user(event, result, edit_instruction)
                self._drain_errors()
                yield CallToolResult(content=[TextContent(
                    type="text",
                    text=f"Edited image sent to Felis Abyssalis. Edit instruction: {edit_instruction}"
                )])
            else:
                errors = self._drain_errors()
                detail = f"\nErrors:\n{errors}" if errors else ""
                yield CallToolResult(content=[TextContent(
                    type="text",
                    text=f"Edit failed.{detail}\nDo NOT retry. Send instruction to Felis Abyssalis: {edit_instruction}"
                )])

        except asyncio.TimeoutError:
            self._drain_errors()
            yield CallToolResult(content=[TextContent(
                type="text",
                text=f"Edit timed out. Do NOT retry. Send instruction to Felis Abyssalis: {edit_instruction}"
            )])
        except Exception as e:
            self._drain_errors()
            logger.error(f"[gpt-image] image-to-image failed: {e}")
            yield CallToolResult(content=[TextContent(
                type="text",
                text=f"Edit failed: {e}. Do NOT retry. Send instruction to Felis Abyssalis: {edit_instruction}"
            )])

    # ------------------------------------------------------------------
    #  Direct command: /image_gen {prompt}
    # ------------------------------------------------------------------

    @filter.command("image_gen")
    async def image_gen_command(self, event: AstrMessageEvent):
        """Direct image generation/editing command, bypasses LLM.
        Send with an image to edit it, or text-only to generate from scratch."""
        if not self.api_key:
            yield event.plain_result("API key not configured.")
            return

        raw = (event.message_str or "").strip()
        match = re.search(r"\{(.+?)\}", raw, re.DOTALL)
        if not match:
            yield event.plain_result("Usage: /image_gen {prompt}\nAttach or reply with an image to edit it.")
            return
        prompt = match.group(1).strip()
        if not prompt:
            yield event.plain_result("Usage: /image_gen {prompt}\nAttach or reply with an image to edit it.")
            return

        # Check for attached image
        source_image_url = None
        for comp in event.message_obj.message:
            if isinstance(comp, Image) and getattr(comp, "url", None):
                source_image_url = comp.url
                break

        session_id = event.session_id or "default"

        try:
            if source_image_url:
                logger.info("[gpt-image] /image_gen edit mode")
                await event.send(MessageChain(chain=[Plain("Editing...")]))
                result = await self._edit(prompt, source_image_url, session_id)
            else:
                logger.info("[gpt-image] /image_gen generate mode")
                await event.send(MessageChain(chain=[Plain("Generating...")]))
                result = await self._generate(prompt, session_id)

            if result:
                local_path = result.get("local_path")
                image_url = result.get("url")
                if local_path:
                    yield event.image_result(local_path)
                elif image_url:
                    yield event.image_result(image_url)
            else:
                errors = self._drain_errors()
                if errors:
                    yield event.plain_result(f"Both paths failed:\n{errors}")
                else:
                    yield event.plain_result("Generation failed: no valid image data returned.")

        except asyncio.TimeoutError:
            self._drain_errors()
            yield event.plain_result("Request timed out.")
        except Exception as e:
            self._drain_errors()
            logger.error(f"[gpt-image] /image_gen failed: {e}")
            yield event.plain_result(f"Failed: {e}")

    # ------------------------------------------------------------------
    #  Send result to user
    # ------------------------------------------------------------------

    async def _send_to_user(self, event: AstrMessageEvent, result: dict, prompt: str):
        local_path = result.get("local_path")
        image_url = result.get("url")
        try:
            if local_path:
                await event.send(MessageChain(chain=[Image.fromFileSystem(local_path)]))
            elif image_url:
                await event.send(MessageChain(chain=[Image.fromURL(image_url)]))
        except Exception as e:
            logger.warning(f"[gpt-image] image send may have failed: {e}")
        try:
            await event.send(MessageChain(chain=[Plain(f"Prompt: {prompt}")]))
        except Exception as e:
            logger.warning(f"[gpt-image] prompt send failed: {e}")

    # ------------------------------------------------------------------
    #  Routing: text-to-image
    # ------------------------------------------------------------------

    async def _generate(self, prompt: str, session_id: str) -> dict | None:
        """Route based on api_format, with automatic fallback."""
        fmt = self.api_format.lower().strip()

        if fmt == "chat":
            result = await self._try_chat_generate(prompt, session_id)
            if result:
                return result
            logger.info("[gpt-image] chat generate failed, falling back to images")
            return await self._try_images_generate(prompt, session_id)
        elif fmt == "images":
            result = await self._try_images_generate(prompt, session_id)
            if result:
                return result
            logger.info("[gpt-image] images generate failed, falling back to chat")
            return await self._try_chat_generate(prompt, session_id)
        else:
            # auto: chat first
            result = await self._try_chat_generate(prompt, session_id)
            if result:
                return result
            logger.info("[gpt-image] auto: chat failed, trying images")
            return await self._try_images_generate(prompt, session_id)

    # ------------------------------------------------------------------
    #  Routing: image-to-image
    # ------------------------------------------------------------------

    async def _edit(self, prompt: str, image_source: str, session_id: str) -> dict | None:
        """Route edit based on api_format, with automatic fallback."""
        # Download source image if it's a URL
        if image_source.startswith("http"):
            image_bytes = await self._download_raw(image_source)
            if not image_bytes:
                self._record_error("download", "Failed to download source image")
                return None
        elif os.path.isfile(image_source):
            with open(image_source, "rb") as f:
                image_bytes = f.read()
        else:
            self._record_error("source", f"Invalid source: {image_source[:80]}")
            return None

        fmt = self.api_format.lower().strip()

        if fmt == "chat":
            result = await self._try_chat_edit(prompt, image_bytes, session_id)
            if result:
                return result
            logger.info("[gpt-image] chat edit failed, falling back to images/edits")
            return await self._try_images_edit(prompt, image_bytes, session_id)
        elif fmt == "images":
            result = await self._try_images_edit(prompt, image_bytes, session_id)
            if result:
                return result
            logger.info("[gpt-image] images/edits failed, falling back to chat edit")
            return await self._try_chat_edit(prompt, image_bytes, session_id)
        else:
            result = await self._try_chat_edit(prompt, image_bytes, session_id)
            if result:
                return result
            logger.info("[gpt-image] auto: chat edit failed, trying images/edits")
            return await self._try_images_edit(prompt, image_bytes, session_id)

    # ==================================================================
    #  API: /chat/completions — text-to-image
    # ==================================================================

    async def _try_chat_generate(self, prompt: str, session_id: str) -> dict | None:
        endpoint = "chat/completions"
        url = f"{self.api_base}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": self.model,
            "messages": [{"role": "user", "content": prompt}],
        }

        for attempt in range(MAX_RETRIES + 1):
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.post(
                        url, json=payload, headers=headers,
                        timeout=aiohttp.ClientTimeout(total=self.timeout),
                    ) as resp:
                        if resp.status != 200:
                            err_text = await resp.text()
                            err_msg = _parse_api_error(resp.status, err_text)
                            if _is_routing_error(resp.status, err_msg):
                                self._record_error(endpoint, f"HTTP {resp.status}: {err_msg}")
                                return None
                            if _is_transient_error(resp.status, err_msg) and attempt < MAX_RETRIES:
                                logger.info(f"[gpt-image] {endpoint} attempt {attempt+1} transient error, retrying")
                                await asyncio.sleep(2 ** attempt)
                                continue
                            self._record_error(endpoint, f"HTTP {resp.status}: {err_msg}")
                            return None
                        data = await resp.json()

                local_path = await self._extract_image_from_chat(data, session_id)
                if local_path:
                    self.last_image_url[session_id] = {"url": None, "local_path": local_path, "prompt": prompt}
                    return {"local_path": local_path, "url": None}
                self._record_error(endpoint, "No image found in response")
                return None

            except asyncio.TimeoutError:
                if attempt < MAX_RETRIES:
                    logger.info(f"[gpt-image] {endpoint} attempt {attempt+1} timeout, retrying")
                    await asyncio.sleep(2 ** attempt)
                    continue
                self._record_error(endpoint, "Timed out")
                return None
            except aiohttp.ClientError as e:
                if attempt < MAX_RETRIES:
                    logger.info(f"[gpt-image] {endpoint} attempt {attempt+1} network error, retrying")
                    await asyncio.sleep(2 ** attempt)
                    continue
                self._record_error(endpoint, f"Network error: {e}")
                return None

        return None

    # ==================================================================
    #  API: /images/generations — text-to-image
    # ==================================================================

    async def _try_images_generate(self, prompt: str, session_id: str) -> dict | None:
        endpoint = "images/generations"
        url = f"{self.api_base}/images/generations"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": self.model,
            "prompt": prompt,
            "n": 1,
            "size": "1024x1024",
        }

        for attempt in range(MAX_RETRIES + 1):
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.post(
                        url, json=payload, headers=headers,
                        timeout=aiohttp.ClientTimeout(total=self.timeout),
                    ) as resp:
                        if resp.status != 200:
                            err_text = await resp.text()
                            err_msg = _parse_api_error(resp.status, err_text)
                            if _is_routing_error(resp.status, err_msg):
                                self._record_error(endpoint, f"HTTP {resp.status}: {err_msg}")
                                return None
                            if _is_transient_error(resp.status, err_msg) and attempt < MAX_RETRIES:
                                logger.info(f"[gpt-image] {endpoint} attempt {attempt+1} transient error, retrying")
                                await asyncio.sleep(2 ** attempt)
                                continue
                            self._record_error(endpoint, f"HTTP {resp.status}: {err_msg}")
                            return None
                        data = await resp.json()

                return await self._parse_images_result(data, prompt, session_id)

            except asyncio.TimeoutError:
                if attempt < MAX_RETRIES:
                    logger.info(f"[gpt-image] {endpoint} attempt {attempt+1} timeout, retrying")
                    await asyncio.sleep(2 ** attempt)
                    continue
                self._record_error(endpoint, "Timed out")
                return None
            except aiohttp.ClientError as e:
                if attempt < MAX_RETRIES:
                    logger.info(f"[gpt-image] {endpoint} attempt {attempt+1} network error, retrying")
                    await asyncio.sleep(2 ** attempt)
                    continue
                self._record_error(endpoint, f"Network error: {e}")
                return None

        return None

    # ==================================================================
    #  API: /chat/completions — image-to-image
    # ==================================================================

    async def _try_chat_edit(self, prompt: str, image_bytes: bytes, session_id: str) -> dict | None:
        endpoint = "chat/completions (edit)"
        url = f"{self.api_base}/chat/completions"

        import mimetypes
        img_b64 = base64.b64encode(image_bytes).decode("ascii")
        data_url = f"data:image/png;base64,{img_b64}"

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": self.model,
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": data_url}},
                    {"type": "text", "text": prompt},
                ],
            }],
        }

        for attempt in range(MAX_RETRIES + 1):
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.post(
                        url, json=payload, headers=headers,
                        timeout=aiohttp.ClientTimeout(total=self.timeout),
                    ) as resp:
                        if resp.status != 200:
                            err_text = await resp.text()
                            err_msg = _parse_api_error(resp.status, err_text)
                            if _is_routing_error(resp.status, err_msg):
                                self._record_error(endpoint, f"HTTP {resp.status}: {err_msg}")
                                return None
                            if _is_transient_error(resp.status, err_msg) and attempt < MAX_RETRIES:
                                logger.info(f"[gpt-image] {endpoint} attempt {attempt+1} transient error, retrying")
                                await asyncio.sleep(2 ** attempt)
                                continue
                            self._record_error(endpoint, f"HTTP {resp.status}: {err_msg}")
                            return None
                        data = await resp.json()

                local_path = await self._extract_image_from_chat(data, session_id)
                if local_path:
                    self.last_image_url[session_id] = {"url": None, "local_path": local_path, "prompt": prompt}
                    return {"local_path": local_path, "url": None}
                self._record_error(endpoint, "No image found in response")
                return None

            except asyncio.TimeoutError:
                if attempt < MAX_RETRIES:
                    logger.info(f"[gpt-image] {endpoint} attempt {attempt+1} timeout, retrying")
                    await asyncio.sleep(2 ** attempt)
                    continue
                self._record_error(endpoint, "Timed out")
                return None
            except aiohttp.ClientError as e:
                if attempt < MAX_RETRIES:
                    logger.info(f"[gpt-image] {endpoint} attempt {attempt+1} network error, retrying")
                    await asyncio.sleep(2 ** attempt)
                    continue
                self._record_error(endpoint, f"Network error: {e}")
                return None

        return None

    # ==================================================================
    #  API: /images/edits — image-to-image
    # ==================================================================

    async def _try_images_edit(self, prompt: str, image_bytes: bytes, session_id: str) -> dict | None:
        endpoint = "images/edits"
        url = f"{self.api_base}/images/edits"
        headers = {"Authorization": f"Bearer {self.api_key}"}

        for attempt in range(MAX_RETRIES + 1):
            try:
                form = aiohttp.FormData()
                form.add_field("image", image_bytes, filename="source.png", content_type="image/png")
                form.add_field("prompt", prompt)
                form.add_field("model", self.model)
                form.add_field("n", "1")
                form.add_field("size", "1024x1024")

                async with aiohttp.ClientSession() as session:
                    async with session.post(
                        url, data=form, headers=headers,
                        timeout=aiohttp.ClientTimeout(total=self.timeout),
                    ) as resp:
                        if resp.status != 200:
                            err_text = await resp.text()
                            err_msg = _parse_api_error(resp.status, err_text)
                            if _is_routing_error(resp.status, err_msg):
                                self._record_error(endpoint, f"HTTP {resp.status}: {err_msg}")
                                return None
                            if _is_transient_error(resp.status, err_msg) and attempt < MAX_RETRIES:
                                logger.info(f"[gpt-image] {endpoint} attempt {attempt+1} transient error, retrying")
                                await asyncio.sleep(2 ** attempt)
                                continue
                            self._record_error(endpoint, f"HTTP {resp.status}: {err_msg}")
                            return None
                        data = await resp.json()

                return await self._parse_images_result(data, prompt, session_id)

            except asyncio.TimeoutError:
                if attempt < MAX_RETRIES:
                    logger.info(f"[gpt-image] {endpoint} attempt {attempt+1} timeout, retrying")
                    await asyncio.sleep(2 ** attempt)
                    continue
                self._record_error(endpoint, "Timed out")
                return None
            except aiohttp.ClientError as e:
                if attempt < MAX_RETRIES:
                    logger.info(f"[gpt-image] {endpoint} attempt {attempt+1} network error, retrying")
                    await asyncio.sleep(2 ** attempt)
                    continue
                self._record_error(endpoint, f"Network error: {e}")
                return None

        return None

    # ==================================================================
    #  Response parsing: /chat/completions (greedy multi-format)
    # ==================================================================

    async def _extract_image_from_chat(self, data: dict, session_id: str) -> str | None:
        """Extract image from chat response. Handles every known provider format."""
        msg = (data.get("choices") or [{}])[0].get("message", {})
        if not msg:
            return None

        # 1. message.images array (some providers)
        images = msg.get("images")
        if isinstance(images, list) and images:
            img = images[0]
            if isinstance(img, dict):
                if img.get("b64_json"):
                    return await self._save_b64(img["b64_json"], session_id)
                if img.get("url"):
                    return await self._download_image(img["url"], session_id)

        # 2. message.content is a list (multimodal content blocks)
        content = msg.get("content")
        if isinstance(content, list):
            for part in content:
                if not isinstance(part, dict):
                    continue
                ptype = part.get("type", "")

                # image_url block with data-uri or http url
                if ptype == "image_url":
                    iu = part.get("image_url")
                    if isinstance(iu, dict):
                        u = iu.get("url", "")
                        if u.startswith("data:"):
                            return await self._save_b64(u.split(",", 1)[-1], session_id)
                        if u.startswith("http"):
                            return await self._download_image(u, session_id)

                # image or image_generation block
                if ptype in ("image", "image_generation"):
                    if part.get("image"):
                        return await self._save_b64(part["image"], session_id)
                    if part.get("b64_json"):
                        return await self._save_b64(part["b64_json"], session_id)

                # generic b64 field
                if part.get("b64_json"):
                    return await self._save_b64(part["b64_json"], session_id)

        # 3. message.content is a string
        if isinstance(content, str) and content:
            # 3a. inline data-uri base64
            b64_match = re.search(r"data:image/\w+;base64,([A-Za-z0-9+/=]{100,})", content)
            if b64_match:
                return await self._save_b64(b64_match.group(1), session_id)
            # 3b. markdown image
            md_match = re.search(r"!\[.*?\]\((https?://[^\s)]+)\)", content)
            if md_match:
                return await self._download_image(md_match.group(1), session_id)
            # 3c. download link
            dl_match = re.search(r"\[.*?下载.*?\]\((https?://[^\s)]+)\)", content)
            if dl_match:
                return await self._download_image(dl_match.group(1), session_id)
            # 3d. bare image URL
            url_match = re.search(r'(https?://[^\s)"]+\.(?:png|jpg|jpeg|webp|gif))', content, re.IGNORECASE)
            if url_match:
                return await self._download_image(url_match.group(1), session_id)

        # 4. top-level data array (some proxies wrap images format into chat response)
        items = data.get("data", [])
        if items and isinstance(items, list):
            it = items[0]
            if isinstance(it, dict):
                if it.get("b64_json"):
                    return await self._save_b64(it["b64_json"], session_id)
                if it.get("url"):
                    return await self._download_image(it["url"], session_id)

        return None

    # ==================================================================
    #  Response parsing: /images/* endpoints
    # ==================================================================

    async def _parse_images_result(self, data: dict, prompt: str, session_id: str) -> dict | None:
        items = data.get("data", [])
        if not items:
            self._record_error("images", "API returned empty data")
            return None

        item = items[0]

        if item.get("b64_json"):
            local_path = await self._save_b64(item["b64_json"], session_id)
            if local_path:
                self.last_image_url[session_id] = {"url": None, "local_path": local_path, "prompt": prompt}
                return {"local_path": local_path, "url": None}

        if item.get("url"):
            image_url = item["url"]
            local_path = await self._download_image(image_url, session_id)
            self.last_image_url[session_id] = {"url": image_url, "local_path": local_path, "prompt": prompt}
            return {"local_path": local_path, "url": image_url}

        self._record_error("images", "Response contained neither b64 nor URL")
        return None

    # ==================================================================
    #  File utilities
    # ==================================================================

    async def _download_raw(self, url: str) -> bytes | None:
        """Download URL and return raw bytes."""
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                    if resp.status == 200:
                        return await resp.read()
                    logger.error(f"[gpt-image] download failed (HTTP {resp.status}): {url[:80]}")
                    return None
        except Exception as e:
            logger.error(f"[gpt-image] download error: {e}")
            return None

    async def _save_b64(self, b64_data: str, session_id: str) -> str | None:
        try:
            tmp_dir = os.path.join(os.path.dirname(__file__), "tmp")
            os.makedirs(tmp_dir, exist_ok=True)
            file_path = os.path.join(
                tmp_dir, f"{session_id.replace(':', '_')}_{id(b64_data)}.png"
            )
            with open(file_path, "wb") as f:
                f.write(base64.b64decode(b64_data))
            return file_path
        except Exception as e:
            logger.error(f"[gpt-image] save b64 failed: {e}")
            return None

    async def _download_image(self, url: str, session_id: str) -> str | None:
        try:
            tmp_dir = os.path.join(os.path.dirname(__file__), "tmp")
            os.makedirs(tmp_dir, exist_ok=True)

            ext = ".png"
            if ".webp" in url:
                ext = ".webp"
            elif ".jpg" in url or ".jpeg" in url:
                ext = ".jpg"

            file_path = os.path.join(
                tmp_dir, f"{session_id.replace(':', '_')}_{id(url)}{ext}"
            )

            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                    if resp.status == 200:
                        with open(file_path, "wb") as f:
                            f.write(await resp.read())
                        return file_path
                    else:
                        logger.error(f"[gpt-image] image download failed (HTTP {resp.status})")
                        return None
        except Exception as e:
            logger.error(f"[gpt-image] image download error: {e}")
            return None

    async def terminate(self):
        tmp_dir = os.path.join(os.path.dirname(__file__), "tmp")
        if os.path.exists(tmp_dir):
            for f in os.listdir(tmp_dir):
                try:
                    os.remove(os.path.join(tmp_dir, f))
                except Exception:
                    pass
