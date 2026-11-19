"""HTTP请求工具模块"""
import contextlib
import aiohttp
from PIL import Image
from io import BytesIO
from astrbot.api import logger


class HttpUtils:
    """HTTP请求工具类"""
    
    @staticmethod
    async def download_image(url: str, timeout: int = 15, 
                             headers: dict = None, proxy: str = None,
                             session: aiohttp.ClientSession | None = None) -> Image.Image:
        """异步下载图片"""
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
                    data = await resp.read()
                    try:
                        img = Image.open(BytesIO(data))
                        img.load()
                        return img
                    except Exception as e:
                        raise Exception(f"图片格式解析失败: {e}")
        except aiohttp.ClientError as e:
            if isinstance(e, Image.DecompressionBombError):
                raise Exception("图片过大，拒绝处理")
            raise e
    
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
