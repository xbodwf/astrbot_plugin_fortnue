"""
今日运势插件 - AstrBot 插件
生成精美的运势图片
"""
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api import logger, llm_tool
import asyncio
from collections import OrderedDict
import contextlib
import os
import tempfile
import weakref
from datetime import datetime
import aiohttp

from .config import ConfigLoader
from .core import BackgroundManager, FortuneGenerator, ImageModerator
from .utils import ImageUtils

_MAX_LAST_BACKGROUNDS = 20
_MAX_LAST_BACKGROUND_BYTES = 6 * 1024 * 1024  # 6 MiB total cap across cached JPEGs
_MAX_CONCURRENT_IMAGE_JOBS = 4


@register("astrbot_plugin_fortnue", "Xbodw", "今日运势生成器 - 生成一张二次元风格的运势图片", "2.1.0")
class FortunePlugin(Star):
    """今日运势插件 - 生成精美的运势图片"""
    
    def __init__(self, context: Context, config: dict = None):
        super().__init__(context)
        self.config = config  # 保存配置
        self.data_dir = os.path.dirname(os.path.abspath(__file__))
        self.backgrounds_path = os.path.join(self.data_dir, "backgrounds.json")

        # 加载配置
        self.backgrounds_data = ConfigLoader.load_backgrounds(self.backgrounds_path)

        # 初始化各模块
        self.fortune_generator = FortuneGenerator(self.data_dir)
        self._http_session: aiohttp.ClientSession | None = None
        self._image_semaphores: weakref.WeakKeyDictionary[
            asyncio.AbstractEventLoop, asyncio.Semaphore
        ] = weakref.WeakKeyDictionary()

        # 上次背景图有界 JPEG 字节缓存（按条目数与总字节数双限制，出图成功后再提交）
        self.user_last_backgrounds: OrderedDict[str, bytes] = OrderedDict()
        self._last_bg_total_bytes: int = 0

        logger.debug("[插件初始化] FortunePlugin 初始化完成")

    def _get_image_semaphore(self) -> asyncio.Semaphore:
        """获取绑定到当前事件循环的出图并发信号量（覆盖取图、审核与渲染全流程）。"""
        loop = asyncio.get_running_loop()
        return self._image_semaphores.setdefault(
            loop, asyncio.Semaphore(_MAX_CONCURRENT_IMAGE_JOBS)
        )

    @staticmethod
    async def _run_worker_holding_slot(
        sem: asyncio.Semaphore,
        func,
        *args,
    ):
        """将阻塞图片处理交给工作线程，并在线程真正结束后再释放并发名额。"""
        worker_task = asyncio.create_task(asyncio.to_thread(func, *args))

        def _on_done(task: asyncio.Task) -> None:
            sem.release()
            if not task.cancelled():
                task.exception()

        worker_task.add_done_callback(_on_done)
        return await asyncio.shield(worker_task)

    def _get_http_session(self) -> aiohttp.ClientSession:
        """惰性创建并复用插件级 aiohttp 会话。"""
        if self._http_session is None or self._http_session.closed:
            connector = None
            try:
                from astrbot.core.utils.http_ssl import build_tls_connector

                connector = build_tls_connector()
            except Exception:
                connector = None
            self._http_session = aiohttp.ClientSession(connector=connector)
        return self._http_session

    async def terminate(self):
        """插件卸载时关闭共享 HTTP 会话并释放缓存。"""
        if self._http_session is not None and not self._http_session.closed:
            await self._http_session.close()
        self._http_session = None
        self.user_last_backgrounds.clear()
        self._last_bg_total_bytes = 0

    def _store_last_background(self, user_id: str, jpeg_bytes: bytes) -> None:
        """出图成功后原子更新有界 JPEG 字节缓存。"""
        if not jpeg_bytes or len(jpeg_bytes) > _MAX_LAST_BACKGROUND_BYTES:
            return
        previous = self.user_last_backgrounds.pop(user_id, None)
        if previous is not None:
            self._last_bg_total_bytes -= len(previous)
        self.user_last_backgrounds[user_id] = jpeg_bytes
        self._last_bg_total_bytes += len(jpeg_bytes)
        while (
            len(self.user_last_backgrounds) > _MAX_LAST_BACKGROUNDS
            or self._last_bg_total_bytes > _MAX_LAST_BACKGROUND_BYTES
        ):
            _, evicted = self.user_last_backgrounds.popitem(last=False)
            self._last_bg_total_bytes -= len(evicted)
    
    def _get_source_name_from_spec(self, bg_spec, bg_manager) -> str | None:
        """从背景规格中提取图源名称

        Args:
            bg_spec: 背景图片规格
            bg_manager: 背景管理器

        Returns:
            图源名称，如果无法确定则返回 None
        """
        if not bg_spec:
            return None

        # 遍历所有图源，找到匹配的
        for source_name, source_data in bg_manager.backgrounds_data.items():
            # 如果 bg_spec 是字符串
            if isinstance(bg_spec, str):
                if isinstance(source_data, list) and bg_spec in source_data:
                    return source_name
                if isinstance(source_data, str) and bg_spec == source_data:
                    return source_name
                if isinstance(source_data, dict):
                    items = source_data.get("items") or source_data.get("urls") or []
                    if isinstance(items, list) and bg_spec in items:
                        return source_name

            # 如果 bg_spec 是字典（API 类型）
            elif isinstance(bg_spec, dict):
                if source_data == bg_spec:
                    return source_name

        return None

    def _get_config(self) -> dict:
        """获取插件配置"""
        # 优先使用 __init__ 接收的 config 参数
        if hasattr(self, "config") and self.config:
            logger.debug("[配置加载] 从 self.config 获取配置")
            return self.config

        # 尝试从 context 获取配置
        if hasattr(self.context, "get_config") and callable(self.context.get_config):
            try:
                config = self.context.get_config()
                if config:
                    logger.debug("[配置加载] 从 context.get_config() 获取配置成功")
                    return config
            except Exception as e:
                logger.warning(f"[配置加载] 从 context.get_config() 获取配置失败: {e}")

        logger.warning("[配置加载] 未找到任何配置，返回空字典")
        return {}
    
    def _get_proxy(self) -> str | None:
        """获取代理配置"""
        return ConfigLoader.get_proxy(self._get_config())
    
    def _get_background_manager(self) -> BackgroundManager:
        """获取背景管理器"""
        fortune_config = ConfigLoader.get_fortune_config(self._get_config())
        return BackgroundManager(
            self.backgrounds_data,
            fortune_config,
            self._get_proxy(),
            session=self._get_http_session(),
        )
    
    def _get_moderator(self) -> ImageModerator:
        """获取图片审查器"""
        config = self._get_config()
        moderation_config = ConfigLoader.get_moderation_config(config)
        moderator = ImageModerator(
            moderation_config,
            self.context,
            self._get_proxy(),
            session=self._get_http_session(),
        )
        logger.debug(f"[插件配置] 审查器已创建，启用状态: {moderator.is_enabled()}")
        return moderator
    
    async def _get_avatar_url(self, event: AstrMessageEvent) -> str:
        """获取用户头像URL"""
        user_id = event.get_sender_id()
        return f"https://q1.qlogo.cn/g?b=qq&nk={user_id}&s=640"

    def _format_fortune_text(self, user_id: str, user_name: str = "") -> str:
        """将运势数据格式化为纯文本"""
        fortune_data = self.fortune_generator.get_fortune_for_user(user_id)
        fortune = fortune_data["fortune"]
        lucky_color = fortune_data["lucky_color"]
        lucky_number = fortune_data["lucky_number"]
        advice = fortune_data["advice"]
        luck_value = fortune_data["luck_value"]
        today_str = datetime.now().strftime("%Y年%m月%d日")

        color_name = lucky_color.get("name", "") if isinstance(lucky_color, dict) else ""
        color_hex = lucky_color.get("hex", "").upper() if isinstance(lucky_color, dict) else ""

        lines = [
            f"✦ {user_name or '你'} 的今日运势 ✦" if user_name else "✦ 今日运势 ✦",
            f"日期：{today_str}",
            f"运势等级：{fortune['level']}",
            f"运势描述：{fortune['desc']}",
            f"幸运指数：{luck_value}%",
            f"幸运色：{color_name}({color_hex})" if color_name else f"幸运色：{color_hex}",
            f"幸运数字：{lucky_number}",
            f"今日建议：{advice}",
            "仅供娱乐 · 相信科学 · 请勿迷信",
        ]
        return "\n".join(lines)
    
    async def _handle_none_generation(self, event: AstrMessageEvent, source_name: str = None):
        """处理纯背景图片获取逻辑"""
        moderator = self._get_moderator()
        max_retries = moderator.get_max_retries() if moderator.is_enabled() else 0

        sem = self._get_image_semaphore()
        await sem.acquire()
        jpeg_bytes = None
        error_msg = None
        slot_transferred = False
        try:
            retries = 0
            last_error = None

            while retries <= max_retries:
                background = None
                transferred_to_worker = False
                try:
                    bg_manager = self._get_background_manager()
                    bg_spec = bg_manager.get_background_spec(source_name)

                    if not bg_spec:
                        if source_name:
                            error_msg = f"未找到指定的图源: {source_name}"
                        else:
                            error_msg = "暂无背景图源配置，请检查 backgrounds.json~"
                        break

                    background, _ = await bg_manager.get_background_and_addition(bg_spec)

                    if not background:
                        raise Exception("无法加载背景图片")

                    # 图片审查
                    if moderator.is_enabled():
                        logger.info(f"[运势生成] 开始图片审查，重试次数: {retries}/{max_retries}")
                        passed, reason, bboxes = await moderator.moderate(background)
                        logger.info(f"[运势生成] 审查结果: passed={passed}, reason={reason}, bboxes={bboxes}")

                        if not passed:
                            failed_action = moderator.get_failed_action()

                            # 打码模式
                            if failed_action == "mosaic" and bboxes:
                                logger.info(f"[运势生成] 打码模式：对 {len(bboxes)} 个区域应用马赛克")
                                mosaiced = ImageUtils.apply_mosaic_multi(
                                    background, bboxes, moderator.get_mosaic_block_size()
                                )
                                if mosaiced is not background:
                                    background.close()
                                    background = mosaiced
                                # 打码后继续使用这张图片
                            else:
                                # 其他模式：重试
                                background.close()
                                background = None
                                logger.warning(f"图片审查未通过: {reason}, 重试中...")
                                retries += 1
                                last_error = f"图片审查未通过: {reason}"
                                continue
                    else:
                        logger.info("[运势生成] 图片审查未启用")

                    def _render_none_to_bytes(src_bg) -> bytes:
                        processed = None
                        try:
                            processed = ImageUtils.process_background(src_bg)
                            src_bg.close()
                            src_bg = None
                            return ImageUtils.encode_jpeg(processed, quality=95)
                        finally:
                            if processed is not None:
                                processed.close()
                            if src_bg is not None:
                                src_bg.close()

                    transferred_to_worker = True
                    slot_transferred = True
                    jpeg_bytes = await self._run_worker_holding_slot(
                        sem, _render_none_to_bytes, background
                    )
                    break

                except Exception as e:
                    logger.error(f"获取背景图片失败: {e}")
                    last_error = str(e)
                    retries += 1
                    if slot_transferred:
                        break
                finally:
                    if not transferred_to_worker and background is not None:
                        background.close()
            else:
                error_msg = f"获取失败: {last_error or '重试次数已达上限'}"

            if jpeg_bytes is None and error_msg is None:
                error_msg = f"获取失败: {last_error or '重试次数已达上限'}"
        finally:
            if not slot_transferred:
                sem.release()

        if error_msg or jpeg_bytes is None:
            yield event.plain_result(error_msg or "获取失败: 未知错误")
            return

        async for res in self._send_jpeg_bytes(event, jpeg_bytes):
            yield res

    async def _send_jpeg_bytes(self, event: AstrMessageEvent, jpeg_bytes: bytes):
        """将 JPEG 字节写入临时文件发送并在发送后清理。"""
        temp_path = None
        try:
            with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as f:
                temp_path = f.name
                f.write(jpeg_bytes)
            yield event.image_result(temp_path)
        finally:
            if temp_path:
                with contextlib.suppress(OSError):
                    os.remove(temp_path)

    async def _handle_fortune_generation(self, event: AstrMessageEvent, source_name: str = None):
        """处理运势图片生成逻辑"""
        user_id = event.get_sender_id()
        user_name = event.get_sender_name()

        moderator = self._get_moderator()
        max_retries = moderator.get_max_retries() if moderator.is_enabled() else 0
        failed_action = moderator.get_failed_action() if moderator.is_enabled() else "retry_same"

        sem = self._get_image_semaphore()
        await sem.acquire()
        result_bytes = None
        bg_bytes = None
        error_msg = None
        slot_transferred = False
        try:
            retries = 0
            last_error = None
            current_source = source_name
            tried_sources = set()  # 记录已尝试的图源

            while retries <= max_retries:
                background = None
                avatar = None
                transferred_to_worker = False
                try:
                    bg_manager = self._get_background_manager()

                    # 如果是切换图源模式且已经尝试过当前图源，则选择新图源
                    if failed_action == "switch_source" and current_source and current_source in tried_sources:
                        logger.info(f"[运势生成] 切换图源模式 - 已尝试过 {current_source}，选择新图源")
                        current_source = None  # 让系统随机选择

                    bg_spec = bg_manager.get_background_spec(current_source)

                    if not bg_spec:
                        if source_name:
                            error_msg = f"未找到指定的图源: {source_name}"
                        else:
                            error_msg = "暂无背景图源配置，请检查 backgrounds.json~"
                        break

                    # 获取实际使用的图源名称
                    actual_source = self._get_source_name_from_spec(bg_spec, bg_manager)
                    if actual_source:
                        tried_sources.add(actual_source)
                        logger.info(f"[运势生成] 使用图源: {actual_source}")

                    background, addition_text = await bg_manager.get_background_and_addition(bg_spec)

                    if not background:
                        raise Exception("无法加载背景图片")

                    # 图片审查 - 检查是否需要审核此图源
                    if moderator.is_enabled() and actual_source:
                        should_moderate = moderator.should_moderate_source(actual_source)

                        if should_moderate:
                            logger.info(f"[运势生成] 图源 {actual_source} 需要审核，开始图片审查，重试次数: {retries}/{max_retries}")
                            passed, reason, bboxes = await moderator.moderate(background)
                            logger.info(f"[运势生成] 审查结果: passed={passed}, reason={reason}, bboxes={bboxes}")

                            if not passed:
                                # 打码模式
                                if failed_action == "mosaic" and bboxes:
                                    logger.info(f"[运势生成] 打码模式：对 {len(bboxes)} 个区域应用马赛克")
                                    mosaiced = ImageUtils.apply_mosaic_multi(
                                        background, bboxes, moderator.get_mosaic_block_size()
                                    )
                                    if mosaiced is not background:
                                        background.close()
                                        background = mosaiced
                                    # 打码后继续使用这张图片
                                else:
                                    background.close()
                                    background = None
                                    logger.warning(f"图片审查未通过: {reason}")
                                    retries += 1
                                    last_error = f"图片审查未通过: {reason}"

                                    # 根据配置的失败行为处理
                                    if failed_action == "notify_user":
                                        error_msg = f"背景图因审查不通过无法生成运势\n原因: {reason}"
                                        break
                                    elif failed_action == "switch_source":
                                        logger.info(f"[运势生成] 失败行为: 切换图源")
                                        current_source = None  # 下次循环会选择新图源
                                    else:  # retry_same
                                        logger.info(f"[运势生成] 失败行为: 从同一图源重新取图")

                                    continue
                        else:
                            logger.info(f"[运势生成] 图源 {actual_source} 在过滤列表中，跳过审核")
                    else:
                        logger.info("[运势生成] 图片审查未启用或无法确定图源")

                    # 获取头像
                    avatar_url = await self._get_avatar_url(event)
                    try:
                        from .utils import HttpUtils
                        avatar = await HttpUtils.download_image(
                            avatar_url,
                            timeout=10,
                            proxy=self._get_proxy(),
                            session=self._get_http_session(),
                        )
                    except Exception as e:
                        logger.warning(f"下载头像失败: {e}，将使用默认空白头像")
                        from PIL import Image
                        avatar = Image.new('RGB', (100, 100), (200, 200, 200))

                    fortune_data = self.fortune_generator.get_fortune_for_user(user_id)

                    def _render_fortune_to_bytes(src_bg, src_avatar) -> tuple[bytes, bytes]:
                        processed = None
                        try:
                            processed = ImageUtils.process_background(src_bg)
                            # 裁成 800x1200 后立刻释放 4K 原图，降低叠字阶段峰值内存
                            src_bg.close()
                            src_bg = None
                            bg_bytes = ImageUtils.encode_jpeg(processed, quality=95)
                            result_image = self.fortune_generator.create_fortune_image(
                                processed,
                                src_avatar,
                                user_name,
                                fortune_data,
                                addition_text,
                            )
                            try:
                                return ImageUtils.encode_jpeg(result_image, quality=90), bg_bytes
                            finally:
                                result_image.close()
                        finally:
                            if processed is not None:
                                processed.close()
                            if src_bg is not None:
                                src_bg.close()
                            if src_avatar is not None:
                                src_avatar.close()

                    transferred_to_worker = True
                    slot_transferred = True
                    result_bytes, bg_bytes = await self._run_worker_holding_slot(
                        sem, _render_fortune_to_bytes, background, avatar
                    )
                    break

                except Exception as e:
                    logger.error(f"生成运势图片失败: {e}")
                    last_error = str(e)
                    retries += 1
                    if slot_transferred:
                        break
                finally:
                    if not transferred_to_worker:
                        if background is not None:
                            background.close()
                        if avatar is not None:
                            avatar.close()
            else:
                error_msg = f"生成失败: {last_error or '重试次数已达上限'}"

            if result_bytes is None and error_msg is None:
                error_msg = f"生成失败: {last_error or '重试次数已达上限'}"
        finally:
            if not slot_transferred:
                sem.release()

        if error_msg or result_bytes is None:
            yield event.plain_result(error_msg or "生成失败: 未知错误")
            return

        # 出图成功后才原子更新 /last 缓存，避免并发覆写或失败污染
        self._store_last_background(user_id, bg_bytes)

        async for res in self._send_jpeg_bytes(event, result_bytes):
            yield res

    @filter.command("jrys", alias=["今日运势", "运势"])
    async def jrys_cmd(self, event: AstrMessageEvent):
        """生成今日运势图片"""
        async for res in self._handle_fortune_generation(event):
            yield res

    @filter.command("jryst", alias=["jrys_text", "运势文本"])
    async def jryst_cmd(self, event: AstrMessageEvent):
        """生成今日运势纯文本（无图片）"""
        user_id = event.get_sender_id()
        user_name = event.get_sender_name()
        yield event.plain_result(self._format_fortune_text(user_id, user_name))

    @llm_tool(name="get_fortnue")
    async def get_fortnue(self, event: AstrMessageEvent, userId: int):
        """获取指定用户的今日运势文本。

        Args:
            userId(int): 用户的ID（如QQ号），用于获取其今日运势
        """
        return self._format_fortune_text(str(userId))

    @filter.command("jrysn")
    async def jrysn(self, event: AstrMessageEvent):
        """随机从图源中获取一张背景图 (无运势信息)"""
        async for res in self._handle_none_generation(event):
            yield res

    @filter.command_group("jrysl")
    async def jrysl(self, event: AstrMessageEvent):
        """今日运势管理指令组"""
        pass

    @jrysl.command("source")
    async def source(self, event: AstrMessageEvent, source_name: str):
        """从指定的图源加载图片生成今日运势"""
        async for res in self._handle_fortune_generation(event, source_name=source_name):
            yield res

    @jrysl.command("last")
    async def last(self, event: AstrMessageEvent):
        """获取上一次今日运势的原始背景图片（无运势信息）"""
        user_id = event.get_sender_id()
        bg_bytes = self.user_last_backgrounds.get(user_id)
        if not bg_bytes:
            yield event.plain_result("你还没有生成过今日运势，请先使用 /jrys 生成一次~")
            return
            
        try:
            self.user_last_backgrounds.move_to_end(user_id)
            async for res in self._send_jpeg_bytes(event, bg_bytes):
                yield res
        except Exception as e:
            logger.error(f"获取上次背景图片失败: {e}")
            yield event.plain_result(f"获取失败: {e}")

    @jrysl.command("restore")
    async def restore(self, event: AstrMessageEvent):
        """重置所有人的运势数据"""
        count = self.fortune_generator.reset_all_fortune_data()
        yield event.plain_result(f"已重置 {count} 位用户的运势数据，下次生成运势将重新随机。")

    @jrysl.group("none")
    async def none(self, event: AstrMessageEvent):
        """单独从图源中刷图（无运势信息）"""
        pass

    @none.command("source")
    async def none_source(self, event: AstrMessageEvent, source_name: str):
        """从指定的图源单独刷图（无运势信息）"""
        async for res in self._handle_none_generation(event, source_name=source_name):
            yield res