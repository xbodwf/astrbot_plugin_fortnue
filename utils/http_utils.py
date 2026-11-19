"""HTTP请求工具模块"""
import asyncio
import contextlib
import aiohttp
from PIL import Image
from io import BytesIO
from astrbot.api import logger

MAX_IMAGE_DOWNLOAD_BYTES = 15 * 1024 * 1024  # 15 MiB
MAX_IMAGE_PIXELS = 25_000_000  # 25M pixels (~5000x5000)


async def read_response_bytes_limited(
    resp: aiohttp.ClientResponse,
    max_bytes: int = MAX_IMAGE_DOWNLOAD_BYTES,
) -> bytes:
    """流式读取响应字节并限制最大体积，防止大图撑爆内存。"""
    content_length = resp.headers.get("Content-Length")
    if content_length:
        try:
            if int(content_length) > max_bytes:
                raise ValueError(f"图片体积超过限制 ({int(content_length)} > {max_bytes} bytes)")
        except ValueError as exc:
            if "图片体积超过限制" in str(exc):
                raise

    chunks = bytearray()
    async for chunk in resp.content.iter_chunked(65536):
        chunks.extend(chunk)
        if len(chunks) > max_bytes:
            raise ValueError(f"图片体积超过限制 (> {max_bytes} bytes)")
    return bytes(chunks)


def decode_image_bytes(
    data: bytes,
    max_pixels: int = MAX_IMAGE_PIXELS,
) -> Image.Image:
    """在校验像素上限后解码图片字节。"""
    buf = BytesIO(data)
    img = None
    try:
        img = Image.open(buf)
        if img.width * img.height > max_pixels:
            img.close()
            raise ValueError(
                f"图片像素过大，拒绝处理 ({img.width}x{img.height} > {max_pixels})"
            )
        img.load()
        return img
    except Image.DecompressionBombError as exc:
        if img is not None:
            img.close()
        raise ValueError("图片过大，拒绝处理") from exc
    except ValueError:
        raise
    except Exception as exc:
        if img is not None:
            img.close()
        raise Exception(f"图片格式解析失败: {exc}") from exc


class HttpUtils:
    """HTTP请求工具类"""
    
    @staticmethod
    async def download_image(url: str, timeout: int = 15, 
                             headers: dict = None, proxy: str = None,
                             session: aiohttp.ClientSession | None = None,
                             max_bytes: int = MAX_IMAGE_DOWNLOAD_BYTES,
                             max_pixels: int = MAX_IMAGE_PIXELS) -> Image.Image:
        """异步下载图片（限流读取 + 线程内限像素解码）"""
        logger.info(f"正在下载图片: {url}")
        base_headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        }
        req_headers = {**base_headers, **headers} if headers else base_headers
        
        session_cm = (
            contextlib.nullcontext(session)
            if session is not None and not session.closed
            else aiohttp.ClientSession()
        )
        try:
            async with session_cm as active_session:
                async with active_session.get(url, timeout=aiohttp.ClientTimeout(total=timeout),
                                              headers=req_headers, proxy=proxy) as resp:
                    if resp.status != 200:
                        raise Exception(f"HTTP {resp.status} {resp.reason}")
                    data = await read_response_bytes_limited(resp, max_bytes=max_bytes)
            return await asyncio.to_thread(decode_image_bytes, data, max_pixels=max_pixels)
        except aiohttp.ClientError:
            raise
    
    @staticmethod
    async def request_json(url: str, method: str = "GET", 
                           headers: dict = None, proxy: str = None,
                           timeout: int = 15, json_data: dict = None,
                           session: aiohttp.ClientSession | None = None) -> dict:
        """发送HTTP请求获取JSON响应"""
        base_headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        }
        req_headers = {**base_headers, **headers} if headers else base_headers
        
        session_cm = (
            contextlib.nullcontext(session)
            if session is not None and not session.closed
            else aiohttp.ClientSession()
        )
        async with session_cm as active_session:
            kwargs = {
                "timeout": aiohttp.ClientTimeout(total=timeout),
                "headers": req_headers,
                "proxy": proxy
            }
            if json_data:
                kwargs["json"] = json_data
            
            async with active_session.request(method.upper(), url, **kwargs) as resp:
                if resp.status != 200:
                    error_text = await resp.text()
                    raise Exception(f"HTTP {resp.status}: {error_text}")
                return await resp.json()
