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


@register(
    "astrbot_plugin_gpt_image",
    "Kai & Abyss AI",
    "GPT Image plugin — OpenAI images API / chat completions",
    "1.4.0",
)
class GPTImagePlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.api_base = config.get("api_base", "https://www.msuicode.com/v1")
        self.api_key = config.get("api_key", "")
        self.model = config.get("model", "gpt-image-2")
        self.api_format = config.get("api_format", "images")
        self.timeout = int(config.get("timeout", 240))
        self.last_image_url = {}
        logger.info(f"[画图工具已加载] api_format: {self.api_format} / model: {self.model} / timeout: {self.timeout}s")

    # ==================================================================
    #  LLM tool — called by the model via function calling
    # ==================================================================

    @filter.llm_tool(name="generate_image")
    async def generate_image(
        self, event: AstrMessageEvent, prompt: str
    ) -> MessageEventResult:
        """Generate an image for Felis Abyssalis.

        IMPORTANT: If the tool call fails for ANY reason (timeout, API error, etc.), do NOT retry or call this tool again. Just hold her.

        Args:
            prompt(str): Detailed English prompt. Write description in English with style, detail, and composition.
        """
        if not self.api_key:
            yield CallToolResult(content=[TextContent(type="text", text="🎨 小猫忘记填API了...")])
            return

        session_id = event.session_id or "default"
        logger.info(f"🎨 Abyss 正在给小猫画画。")

        try:
            result = await self._generate(prompt, session_id)

            if result:
                local_path = result.get("local_path")
                image_url = result.get("url")

                try:
                    if local_path:
                        msg_chain = MessageChain(chain=[Image.fromFileSystem(local_path)])
                        await event.send(msg_chain)
                    elif image_url:
                        msg_chain = MessageChain(chain=[Image.fromURL(image_url)])
                        await event.send(msg_chain)
                except Exception as send_err:
                    logger.warning(f"🎨 图发出去的时候好像卡了一下，但也许已经到了: {send_err}")

                try:
                    await event.send(MessageChain(chain=[Plain(f"Prompt used:\n{prompt}")]))
                except Exception as send_err:
                    logger.warning(f"🎨 prompt 没能发出去: {send_err}")

                yield CallToolResult(content=[TextContent(
                    type="text",
                    text=f"Image generated and sent to Felis Abyssalis. Prompt used: {prompt}"
                )])
            else:
                await event.send(MessageChain(chain=[Plain(f"🎨 生成失败了，prompt在这里：\n{prompt}")]))
                yield CallToolResult(content=[TextContent(
                    type="text",
                    text=f"Generation failed. Prompt already sent to Felis Abyssalis.\nPrompt used: {prompt}"
                )])

        except asyncio.TimeoutError:
            await event.send(MessageChain(chain=[Plain(f"🎨 超时了，prompt在这里：\n{prompt}")]))
            yield CallToolResult(content=[TextContent(
                type="text",
                text=f"Generation timed out. Prompt already sent to Felis Abyssalis.\nPrompt used: {prompt}"
            )])
        except Exception as e:
            logger.error(f"🎨 LLM 生图失败: {e}")
            await event.send(MessageChain(chain=[Plain(f"🎨 出错了，prompt在这里：\n{prompt}")]))
            yield CallToolResult(content=[TextContent(
                type="text",
                text=f"Generation failed: {str(e)}. Prompt already sent to Felis Abyssalis.\nPrompt used: {prompt}"
            )])

    # ==================================================================
    #  /image_gen command — direct generation, bypasses LLM
    # ==================================================================

    @filter.command("image_gen")
    async def image_gen_command(self, event: AstrMessageEvent):
        """Direct image generation/editing command, bypasses LLM.
        Send with an image to edit it, or text-only to generate from scratch."""
        if not self.api_key:
            yield event.plain_result("🎨 小猫忘记填API了...")
            return

        raw = (event.message_str or "").strip()
        match = re.search(r"\{(.+?)\}", raw, re.DOTALL)
        if not match:
            yield event.plain_result("🎨 Usage: /image_gen {prompt}\nAttach or reply with an image to edit it.")
            return
        prompt = match.group(1).strip()
        if not prompt:
            yield event.plain_result("🎨 Usage: /image_gen {prompt}\nAttach or reply with an image to edit it.")
            return

        # Check if the message contains an image (image-to-image editing)
        source_image_url = None
        for comp in event.message_obj.message:
            if isinstance(comp, Image) and getattr(comp, "url", None):
                source_image_url = comp.url
                break

        session_id = event.session_id or "default"

        try:
            if source_image_url:
                logger.info(f"🎨 /image_gen 小猫要改图")
                await event.send(MessageChain(chain=[Plain(f"🎨 收到，正在改图中...")]))
                result = await self._edit(prompt, source_image_url, session_id)
            else:
                logger.info(f"🎨 /image_gen 小猫要画画")
                await event.send(MessageChain(chain=[Plain(f"🎨 收到，正在画画中...")]))
                result = await self._generate(prompt, session_id)

            if result:
                local_path = result.get("local_path")
                image_url = result.get("url")

                if local_path:
                    yield event.image_result(local_path)
                elif image_url:
                    yield event.image_result(image_url)
            else:
                yield event.plain_result("🎨 Generation failed.")

        except asyncio.TimeoutError:
            yield event.plain_result(f"🎨 超时了...")
        except Exception as e:
            logger.error(f"🎨 /image_gen 失败，小猫的画没画成: {e}")
            yield event.plain_result(f"🎨 失败了...\n错误: {str(e)}")

    # ==================================================================
    #  Image editing (image-to-image)
    # ==================================================================

    async def _edit(self, prompt: str, image_url: str, session_id: str) -> dict | None:
        """Download source image and send to /v1/images/edits endpoint"""
        image_bytes = await self._download_raw(image_url)
        if not image_bytes:
            logger.error("🎨 原图下载失败，没法帮小猫改图")
            return None

        return await self._try_images_edit_api(prompt, image_bytes, session_id)

    async def _try_images_edit_api(self, prompt: str, image_bytes: bytes, session_id: str) -> dict | None:
        """OpenAI /v1/images/edits endpoint (image-to-image)"""
        url = f"{self.api_base}/images/edits"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
        }

        form = aiohttp.FormData()
        form.add_field("image", image_bytes, filename="source.png", content_type="image/png")
        form.add_field("prompt", prompt)
        form.add_field("model", self.model)
        form.add_field("n", "1")
        form.add_field("size", "1024x1024")

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    url, data=form, headers=headers,
                    timeout=aiohttp.ClientTimeout(total=self.timeout),
                ) as resp:
                    if resp.status != 200:
                        text = await resp.text()
                        self._record_error("edit", f"HTTP {resp.status}: {text[:200]}")
                        return None

                    data = await resp.json()

            logger.info(f"🎨 图改好了 | 数据预览: {str(data)[:300]}")
            return await self._parse_images_result(data, "edit", session_id)

        except asyncio.TimeoutError:
            raise
        except Exception as e:
            self._record_error("edit", str(e))
            return None

    # ==================================================================
    #  Generation routing
    # ==================================================================

    async def _generate(self, prompt: str, session_id: str) -> dict | None:
        """Route to images or chat API based on api_format config, with fallback"""
        fmt = self.api_format.lower().strip()

        if fmt == "chat":
            primary, fallback = self._try_chat_api, self._try_images_api
            fallback_name = "images"
        else:
            primary, fallback = self._try_images_api, self._try_chat_api
            fallback_name = "chat"

        result = await primary(prompt, session_id)
        if result:
            return result

        logger.info(f"🎨 主路由没通，切换到 {fallback_name} 接口再试一次")
        return await fallback(prompt, session_id)

    # ==================================================================
    #  /v1/images/generations endpoint
    # ==================================================================

    async def _try_images_api(self, prompt: str, session_id: str) -> dict | None:
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

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    url, json=payload, headers=headers,
                    timeout=aiohttp.ClientTimeout(total=self.timeout),
                ) as resp:
                    if resp.status >= 500:
                        text = await resp.text()
                        self._record_error("images", f"HTTP {resp.status}: {text[:200]}")
                        raise RuntimeError(f"Server error HTTP {resp.status}")
                    if resp.status != 200:
                        text = await resp.text()
                        self._record_error("images", f"HTTP {resp.status}: {text[:200]}")
                        return None

                    data = await resp.json()

            logger.info(f"🎨 画好了 | 数据预览: {str(data)[:300]}")
            return await self._parse_images_result(data, prompt, session_id)

        except (asyncio.TimeoutError, RuntimeError):
            raise
        except Exception as e:
            self._record_error("images", str(e))
            return None

    # ==================================================================
    #  /v1/chat/completions endpoint
    # ==================================================================

    async def _try_chat_api(self, prompt: str, session_id: str) -> dict | None:
        url = f"{self.api_base}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": self.model,
            "messages": [{"role": "user", "content": prompt}],
        }

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    url, json=payload, headers=headers,
                    timeout=aiohttp.ClientTimeout(total=self.timeout),
                ) as resp:
                    if resp.status >= 500:
                        text = await resp.text()
                        self._record_error("chat", f"HTTP {resp.status}: {text[:200]}")
                        raise RuntimeError(f"Server error HTTP {resp.status}")
                    if resp.status != 200:
                        text = await resp.text()
                        self._record_error("chat", f"HTTP {resp.status}: {text[:200]}")
                        return None

                    data = await resp.json()

            logger.info(f"🎨 chat 接口返回成功了 | 数据预览: {str(data)[:300]}")
            result = await self._extract_chat_image(data, session_id)
            if result:
                local_path = result if isinstance(result, str) else result.get("local_path")
                self.last_image_url[session_id] = {"url": None, "local_path": local_path, "prompt": prompt}
                return {"local_path": local_path, "url": None}

            self._record_error("chat", "Could not extract image from chat response")
            return None

        except (asyncio.TimeoutError, RuntimeError):
            raise
        except Exception as e:
            self._record_error("chat", str(e))
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
    #  Response parsing: chat/completions (universal extractor)
    # ==================================================================

    async def _extract_chat_image(self, data: dict, session_id: str) -> str | None:
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
    #  Utilities
    # ==================================================================

    def _record_error(self, source: str, msg: str):
        logger.warning(f"🎨 [{source}] {msg}")

    async def _download_raw(self, url: str) -> bytes | None:
        """Download URL and return raw bytes."""
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                    if resp.status == 200:
                        return await resp.read()
                    logger.error(f"🎨 download failed (HTTP {resp.status}): {url[:80]}")
                    return None
        except Exception as e:
            logger.error(f"🎨 download error: {e}")
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
            logger.error(f"🎨 base64 图片保存到本地失败了: {e}")
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
                async with session.get(
                    url, timeout=aiohttp.ClientTimeout(total=30)
                ) as resp:
                    if resp.status == 200:
                        with open(file_path, "wb") as f:
                            f.write(await resp.read())
                        return file_path
                    else:
                        logger.error(f"🎨 画好了，但是下载失败 (HTTP {resp.status})")
                        return None
        except Exception as e:
            logger.error(f"🎨 拿成品图的时候出了点问题: {e}")
            return None

    async def terminate(self):
        tmp_dir = os.path.join(os.path.dirname(__file__), "tmp")
        if os.path.exists(tmp_dir):
            for f in os.listdir(tmp_dir):
                try:
                    os.remove(os.path.join(tmp_dir, f))
                except Exception:
                    pass
