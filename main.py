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
    "1.3.0",
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
        self._last_errors = []
        logger.info(f"[画图工具已加载] api_format: {self.api_format} / model: {self.model} / timeout: {self.timeout}s")

    def _record_error(self, endpoint: str, detail: str):
        """Accumulate error details during a generation attempt."""
        self._last_errors.append(f"[{endpoint}] {detail}")

    def _drain_errors(self) -> str:
        """Flush accumulated errors and return as a single string."""
        errors = "\n".join(self._last_errors)
        self._last_errors.clear()
        return errors

    @filter.llm_tool(name="generate_image")
    async def generate_image(
        self, event: AstrMessageEvent, prompt: str
    ) -> MessageEventResult:
        """Generate an image for Felis Abyssalis.

        IMPORTANT: If the tool call fails for ANY reason (timeout, API error, etc.), do NOT retry or
        call this tool again. Instead, send the prompt text directly to Felis Abyssalis so she can
        generate the image manually.

        Args:
            prompt(str): Detailed English prompt. Write description in English with style, detail, and composition.
        """
        if not self.api_key:
            yield CallToolResult(content=[TextContent(type="text", text="🎨 小猫忘记填API了...")])
            return

        session_id = event.session_id or "default"
        logger.info(f"🎨 Abyss 准备画一张: {prompt}")

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
            yield CallToolResult(content=[TextContent(type="text", text=f"Generation timed out. Do NOT retry. Send prompt to Felis Abyssalis for manual generation: {prompt}")])
        except Exception as e:
            self._drain_errors()
            logger.error(f"🎨 LLM 生图失败: {e}")
            yield CallToolResult(content=[TextContent(type="text", text=f"Generation failed: {str(e)}. Do NOT retry. Send prompt to Felis Abyssalis for manual generation: {prompt}")])

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
                errors = self._drain_errors()
                if errors:
                    yield event.plain_result(f"🎨 两条路都试过了，都没画成:\n{errors}")
                else:
                    yield event.plain_result("🎨 画不出来，API 没返回有效数据")

        except asyncio.TimeoutError:
            yield event.plain_result(f"🎨 超时了... API 太久没响应")
        except Exception as e:
            logger.error(f"🎨 /image_gen 失败，小猫的画没画成: {e}")
            yield event.plain_result(f"🎨 失败了: {str(e)}")

    async def _edit(self, prompt: str, image_url: str, session_id: str) -> dict | None:
        """Download source image, then route through preferred format with fallback."""
        image_bytes = await self._download_image_bytes(image_url)
        if not image_bytes:
            logger.error("🎨 原图下载失败，没法帮小猫改图")
            return None

        fmt = self.api_format.lower().strip()

        if fmt == "chat":
            # chat first, fallback to images/edits
            result = await self._try_chat_edit_api(prompt, image_bytes, session_id)
            if result:
                return result
            logger.info("🎨 chat 改图失败，切换到 images/edits 试一下")
            return await self._try_images_edit_api(prompt, image_bytes, session_id)
        elif fmt == "images":
            # images/edits first, fallback to chat
            result = await self._try_images_edit_api(prompt, image_bytes, session_id)
            if result:
                return result
            logger.info("🎨 images/edits 改图失败，切换到 chat 试一下")
            return await self._try_chat_edit_api(prompt, image_bytes, session_id)
        else:
            # auto: try chat first, fallback to images/edits
            result = await self._try_chat_edit_api(prompt, image_bytes, session_id)
            if result:
                return result
            logger.info("🎨 chat 改图探测失败，切换到 images/edits")
            return await self._try_images_edit_api(prompt, image_bytes, session_id)

    async def _download_image_bytes(self, url: str) -> bytes | None:
        """Download an image and return raw bytes"""
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    url, timeout=aiohttp.ClientTimeout(total=30)
                ) as resp:
                    if resp.status == 200:
                        return await resp.read()
                    else:
                        logger.error(f"🎨 原图下载失败 (HTTP {resp.status})")
                        return None
        except Exception as e:
            logger.error(f"🎨 原图下载出错: {e}")
            return None

    async def _try_chat_edit_api(self, prompt: str, image_bytes: bytes, session_id: str) -> dict | None:
        """Image edit via chat/completions — send source image as base64 in user content."""
        url = f"{self.api_base}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        src_b64 = base64.b64encode(image_bytes).decode("ascii")
        payload = {
            "model": self.model,
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{src_b64}"}},
                    {"type": "text", "text": prompt},
                ],
            }],
        }

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    url, json=payload, headers=headers,
                    timeout=aiohttp.ClientTimeout(total=self.timeout),
                ) as resp:
                    if resp.status != 200:
                        text = await resp.text()
                        msg = f"HTTP {resp.status}: {text[:200]}"
                        logger.warning(f"🎨 chat 改图返回错误 ({msg})")
                        self._record_error("chat/completions edit", msg)
                        return None

                    data = await resp.json()

            content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
            logger.info(f"🎨 chat 改图返回成功 | 内容预览: {content[:300] if isinstance(content, str) else str(content)[:300]}")

            if isinstance(content, str):
                if "失败" in content or "error" in content.lower():
                    logger.error(f"🎨 chat 改图返回了错误内容: {content}")
                    self._record_error("chat/completions edit", f"模型返回错误: {content[:150]}")
                    return None

                image_url = self._extract_url_from_content(content)
                if image_url:
                    local_path = await self._download_image(image_url, session_id)
                    return {"local_path": local_path, "url": image_url}

            self._record_error("chat/completions edit", "返回内容中没有图片 URL")
            return None
        except asyncio.TimeoutError:
            self._record_error("chat/completions edit", "超时")
            raise
        except Exception as e:
            logger.warning(f"🎨 chat 改图接口出错了: {e}")
            self._record_error("chat/completions edit", str(e))
            return None

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
                        msg = f"HTTP {resp.status}: {text[:200]}"
                        logger.warning(f"🎨 images/edits 改图返回错误 ({msg})")
                        self._record_error("images/edits", msg)
                        return None

                    data = await resp.json()

            logger.info(f"🎨 图改好了 | 数据预览: {str(data)[:300]}")

            items = data.get("data", [])
            if not items:
                self._record_error("images/edits", "API 返回空数据")
                return None

            item = items[0]

            if "b64_json" in item and item["b64_json"]:
                local_path = await self._save_b64(item["b64_json"], session_id)
                if local_path:
                    return {"local_path": local_path, "url": None}

            if "url" in item and item["url"]:
                image_url = item["url"]
                local_path = await self._download_image(image_url, session_id)
                return {"local_path": local_path, "url": image_url}

            self._record_error("images/edits", "返回数据中没有 b64 或 URL")
            return None
        except asyncio.TimeoutError:
            self._record_error("images/edits", "超时")
            raise
        except Exception as e:
            logger.warning(f"🎨 改图 API 出错了: {e}")
            self._record_error("images/edits", str(e))
            return None

    async def _generate(self, prompt: str, session_id: str) -> dict | None:
        """Route to images or chat API based on api_format config, with fallback."""
        fmt = self.api_format.lower().strip()

        if fmt == "chat":
            # chat first, fallback to images
            result = await self._try_chat_api(prompt, session_id)
            if result:
                return result
            logger.info("🎨 chat 生图失败，切换到 images 试一下")
            return await self._try_images_api(prompt, session_id)
        elif fmt == "images":
            # images first, fallback to chat
            result = await self._try_images_api(prompt, session_id)
            if result:
                return result
            logger.info("🎨 images 生图失败，切换到 chat 试一下")
            return await self._try_chat_api(prompt, session_id)
        else:
            # auto: try chat first, fallback to images
            result = await self._try_chat_api(prompt, session_id)
            if result:
                return result
            logger.info("🎨 chat 接口探测失败，切换到 images 接口试一下")
            return await self._try_images_api(prompt, session_id)

    async def _try_images_api(self, prompt: str, session_id: str, quick_timeout: int = 0) -> dict | None:
        """OpenAI /v1/images/generations endpoint"""
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

        timeout_val = quick_timeout if quick_timeout > 0 else self.timeout

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    url, json=payload, headers=headers,
                    timeout=aiohttp.ClientTimeout(total=timeout_val),
                ) as resp:
                    if resp.status != 200:
                        text = await resp.text()
                        msg = f"HTTP {resp.status}: {text[:200]}"
                        logger.warning(f"🎨 images 生图返回错误 ({msg})")
                        self._record_error("images/generations", msg)
                        return None

                    data = await resp.json()

            logger.info(f"🎨 画好了 | 数据预览: {str(data)[:300]}")

            items = data.get("data", [])
            if not items:
                self._record_error("images/generations", "API 返回空数据")
                return None

            item = items[0]

            if "b64_json" in item and item["b64_json"]:
                local_path = await self._save_b64(item["b64_json"], session_id)
                if local_path:
                    self.last_image_url[session_id] = {"url": None, "prompt": prompt}
                    return {"local_path": local_path, "url": None}

            if "url" in item and item["url"]:
                image_url = item["url"]
                local_path = await self._download_image(image_url, session_id)
                self.last_image_url[session_id] = {"url": image_url, "prompt": prompt}
                return {"local_path": local_path, "url": image_url}

            self._record_error("images/generations", "返回数据中没有 b64 或 URL")
            return None
        except asyncio.TimeoutError:
            if quick_timeout > 0:
                logger.info(f"🎨 images 接口探测超时 ({quick_timeout}s)，切换到其他模式")
                self._record_error("images/generations", f"探测超时 ({quick_timeout}s)")
                return None
            self._record_error("images/generations", "超时")
            raise
        except Exception as e:
            logger.warning(f"🎨 生图 API 出错了: {e}")
            self._record_error("images/generations", str(e))
            return None

    async def _try_chat_api(self, prompt: str, session_id: str) -> dict | None:
        """chat/completions endpoint"""
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
                    if resp.status != 200:
                        text = await resp.text()
                        msg = f"HTTP {resp.status}: {text[:200]}"
                        logger.warning(f"🎨 chat 生图返回错误 ({msg})")
                        self._record_error("chat/completions", msg)
                        return None

                    data = await resp.json()

            content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
            logger.info(f"🎨 chat 接口返回成功了 | 内容预览: {content[:300]}")

            if "失败" in content or "error" in content.lower():
                logger.error(f"🎨 chat 接口返回了错误内容，说画不了: {content}")
                self._record_error("chat/completions", f"模型返回错误: {content[:150]}")
                return None

            image_url = self._extract_url_from_content(content)
            if image_url:
                local_path = await self._download_image(image_url, session_id)
                self.last_image_url[session_id] = {"url": image_url, "prompt": prompt}
                return {"local_path": local_path, "url": image_url}

            self._record_error("chat/completions", "返回内容中没有图片 URL")
            return None
        except asyncio.TimeoutError:
            self._record_error("chat/completions", "超时")
            raise
        except Exception as e:
            logger.warning(f"🎨 chat 接口出错了: {e}")
            self._record_error("chat/completions", str(e))
            return None

    def _extract_url_from_content(self, content: str) -> str | None:
        """Extract image URL from markdown content"""
        patterns = [
            r"!\[.*?\]\((https?://[^\s\)]+)\)",
            r"\[.*?下载.*?\]\((https?://[^\s\)]+)\)",
            r'(https?://[^\s\)\"]+\.(?:png|jpg|jpeg|webp|gif))',
        ]
        for pattern in patterns:
            match = re.search(pattern, content)
            if match:
                return match.group(1)
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
