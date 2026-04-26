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
    "Abyss 专属画图插件 — GPT Image via OpenAI images API / chat completions",
    "1.2.1",
)
class GPTImagePlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.api_base = config.get("api_base", "https://api.tu-zi.com/v1")
        self.api_key = config.get("api_key", "")
        self.model = config.get("model", "gpt-image-2")
        self.api_format = config.get("api_format", "images")
        self.timeout = int(config.get("timeout", 240))
        self.last_image_url = {}
        logger.info(f"GPT Image 插件加载: api_format={self.api_format}, model={self.model}, timeout={self.timeout}")

    @filter.llm_tool(name="generate_image")
    async def generate_image(
        self, event: AstrMessageEvent, prompt: str
    ) -> MessageEventResult:
        """画图。当 Felis Abyssalis 想要画图、生成图片、创建图像时调用。

        Args:
            prompt(str): 详细的英文 prompt。将 Felis Abyssalis 的描述翻译为英文，包含风格、细节、构图等信息。
        """
        if not self.api_key:
            yield CallToolResult(content=[TextContent(type="text", text="还没有配置 API Key，去插件设置里填一下。")])
            return

        session_id = event.session_id or "default"
        logger.info(f"GPT Image 生成请求 (format={self.api_format}): {prompt}")

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
                    logger.warning(f"图片发送可能超时（但图片可能已成功发出）: {send_err}")

                try:
                    await event.send(MessageChain(chain=[Plain(f"Prompt: {prompt}")]))
                except Exception as send_err:
                    logger.warning(f"Prompt 发送失败: {send_err}")

                yield CallToolResult(content=[TextContent(
                    type="text",
                    text=f"[图片已生成并发送，不需要再发送图片] prompt: {prompt}"
                )])
            else:
                yield CallToolResult(content=[TextContent(
                    type="text",
                    text="没画出来，API 没返回有效图片。可能是服务端在忙，等一下再试。"
                )])

        except asyncio.TimeoutError:
            yield CallToolResult(content=[TextContent(type="text", text="画图请求超时了，等一下再试。")])
        except Exception as e:
            logger.error(f"GPT Image 生成失败: {e}")
            yield CallToolResult(content=[TextContent(type="text", text=f"画图失败了: {str(e)}")])

    @filter.llm_tool(name="edit_image")
    async def edit_image(
        self, event: AstrMessageEvent, edit_instruction: str
    ) -> MessageEventResult:
        """修改上一次画的图。当 Felis Abyssalis 想要修改刚才生成的图片时调用。

        Args:
            edit_instruction(str): 英文修改指令。将修改要求翻译为英文，结合上一次的 prompt 生成新的完整描述。
        """
        if not self.api_key:
            yield CallToolResult(content=[TextContent(type="text", text="还没有配置 API Key，去插件设置里填一下。")])
            return

        session_id = event.session_id or "default"
        last = self.last_image_url.get(session_id)

        if not last:
            yield CallToolResult(content=[TextContent(type="text", text="还没有上一张图的记录，先画一张再来改。")])
            return

        new_prompt = edit_instruction
        logger.info(f"GPT Image 修改请求: {new_prompt}")

        try:
            result = await self._generate(new_prompt, session_id)

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
                    logger.warning(f"图片发送可能超时（但图片可能已成功发出）: {send_err}")

                try:
                    await event.send(MessageChain(chain=[Plain(f"Prompt: {new_prompt}")]))
                except Exception as send_err:
                    logger.warning(f"Prompt 发送失败: {send_err}")

                yield CallToolResult(content=[TextContent(
                    type="text",
                    text=f"[修改后的图片已发送，不需要再发送图片] 原 prompt: {last['prompt']}，新 prompt: {new_prompt}"
                )])
            else:
                yield CallToolResult(content=[TextContent(type="text", text="没改出来，等一下再试。")])

        except asyncio.TimeoutError:
            yield CallToolResult(content=[TextContent(type="text", text="改图请求超时了，等一下再试。")])
        except Exception as e:
            logger.error(f"GPT Image 修改失败: {e}")
            yield CallToolResult(content=[TextContent(type="text", text=f"改图失败了: {str(e)}")])

    async def _generate(self, prompt: str, session_id: str) -> dict | None:
        """根据配置的 api_format 选择对应的生成方式"""
        fmt = self.api_format.lower().strip()

        if fmt == "images":
            return await self._try_images_api(prompt, session_id)
        elif fmt == "chat":
            return await self._try_chat_api(prompt, session_id)
        else:
            # auto: 先快速试 images（15秒超时），失败再试 chat
            result = await self._try_images_api(prompt, session_id, quick_timeout=15)
            if result:
                return result
            logger.info("images API 快速尝试未成功，切换到 chat API")
            return await self._try_chat_api(prompt, session_id)

    async def _try_images_api(self, prompt: str, session_id: str, quick_timeout: int = 0) -> dict | None:
        """标准 OpenAI /v1/images/generations 端点"""
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
                        logger.warning(f"images API 失败 ({resp.status}): {text[:200]}")
                        return None

                    data = await resp.json()

            logger.info(f"images API 返回: {str(data)[:300]}")

            items = data.get("data", [])
            if not items:
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

            return None
        except asyncio.TimeoutError:
            if quick_timeout > 0:
                logger.info(f"images API 快速探测超时 ({quick_timeout}s)，将尝试其他格式")
                return None
            raise
        except Exception as e:
            logger.warning(f"images API 异常: {e}")
            return None

    async def _try_chat_api(self, prompt: str, session_id: str) -> dict | None:
        """chat/completions 端点"""
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
                        logger.warning(f"chat API 失败 ({resp.status}): {text[:200]}")
                        return None

                    data = await resp.json()

            content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
            logger.info(f"chat API 返回 content: {content[:300]}")

            if "失败" in content or "error" in content.lower():
                logger.error(f"chat API 返回错误: {content}")
                return None

            image_url = self._extract_url_from_content(content)
            if image_url:
                local_path = await self._download_image(image_url, session_id)
                self.last_image_url[session_id] = {"url": image_url, "prompt": prompt}
                return {"local_path": local_path, "url": image_url}

            return None
        except asyncio.TimeoutError:
            raise
        except Exception as e:
            logger.warning(f"chat API 异常: {e}")
            return None

    def _extract_url_from_content(self, content: str) -> str | None:
        """从 markdown 文本中提取图片URL"""
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
            logger.error(f"保存 b64 图片失败: {e}")
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
                        logger.error(f"图片下载失败 ({resp.status}): {url}")
                        return None
        except Exception as e:
            logger.error(f"图片下载异常: {e}")
            return None

    async def terminate(self):
        tmp_dir = os.path.join(os.path.dirname(__file__), "tmp")
        if os.path.exists(tmp_dir):
            for f in os.listdir(tmp_dir):
                try:
                    os.remove(os.path.join(tmp_dir, f))
                except Exception:
                    pass
