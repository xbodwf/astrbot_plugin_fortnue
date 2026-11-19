"""图片处理工具模块"""
from PIL import Image, ImageDraw
from io import BytesIO
import base64


class ImageUtils:
    """图片处理工具类"""
    
    @staticmethod
    def make_circle_image(img: Image.Image, size: tuple) -> Image.Image:
        """将图片裁剪成圆形"""
        resized = img.resize(size, Image.Resampling.LANCZOS)
        rgba = resized.convert('RGBA') if resized.mode != 'RGBA' else resized
        mask = Image.new('L', size, 0)
        try:
            draw = ImageDraw.Draw(mask)
            draw.ellipse([0, 0, size[0], size[1]], fill=255)
            result = Image.new('RGBA', size, (0, 0, 0, 0))
            result.paste(rgba, mask=mask)
            return result
        finally:
            mask.close()
            if rgba is not resized:
                rgba.close()
            resized.close()
    
    @staticmethod
    def add_avatar_border(avatar: Image.Image, border_width: int = 4,
                          border_color: tuple = (255, 255, 255)) -> Image.Image:
        """为圆形头像添加边框"""
        new_size = (avatar.width + border_width * 2, avatar.height + border_width * 2)
        bordered = Image.new('RGBA', new_size, (0, 0, 0, 0))
        mask = Image.new('L', new_size, 0)
        border_layer = Image.new('RGBA', new_size, border_color + (255,))
        try:
            draw = ImageDraw.Draw(mask)
            draw.ellipse([0, 0, new_size[0], new_size[1]], fill=255)
            bordered.paste(border_layer, mask=mask)
            bordered.paste(avatar, (border_width, border_width), avatar)
            return bordered
        finally:
            border_layer.close()
            mask.close()
    
    @staticmethod
    def encode_jpeg(img: Image.Image, quality: int = 90) -> bytes:
        """将图像编码为 JPEG 字节并及时释放临时缓冲区。"""
        buffered = BytesIO()
        converted = img.convert('RGB') if img.mode != 'RGB' else None
        try:
            (converted or img).save(buffered, format="JPEG", quality=quality)
            return buffered.getvalue()
        finally:
            if converted is not None:
                converted.close()
            buffered.close()

    @staticmethod
    def image_to_base64(img: Image.Image, quality: int = 85) -> str:
        """将图片转换为 base64 编码"""
        return base64.b64encode(ImageUtils.encode_jpeg(img, quality=quality)).decode('utf-8')
    
    @staticmethod
    def process_background(bg: Image.Image, target_width: int = 800, 
                           target_height: int = 1200) -> Image.Image:
        """一步居中裁剪并缩放到目标尺寸，保留图像模式与透明度。"""
        bg_ratio = bg.width / bg.height
        target_ratio = target_width / target_height
        
        if bg_ratio > target_ratio:
            new_width = int(bg.height * target_ratio)
            left = (bg.width - new_width) // 2
            crop_box = (left, 0, left + new_width, bg.height)
        else:
            new_height = int(bg.width / target_ratio)
            top = (bg.height - new_height) // 2
            crop_box = (0, top, bg.width, top + new_height)
        
        return bg.resize(
            (target_width, target_height),
            Image.Resampling.LANCZOS,
            box=crop_box,
        )
    
    @staticmethod
    def _apply_mosaic_in_place(
        target: Image.Image,
        bbox: tuple,
        block_size: int = 15,
    ) -> None:
        """在目标 RGB 图像上原地对单个区域应用 Pillow 原生马赛克。"""
        width, height = target.size
        x1, y1, x2, y2 = bbox

        if all(0 <= v <= 1 for v in bbox):
            x1 = int(x1 * width)
            y1 = int(y1 * height)
            x2 = int(x2 * width)
            y2 = int(y2 * height)

        x1 = max(0, min(int(x1), width))
        y1 = max(0, min(int(y1), height))
        x2 = max(0, min(int(x2), width))
        y2 = max(0, min(int(y2), height))

        rw = x2 - x1
        rh = y2 - y1
        if rw <= 0 or rh <= 0:
            return

        step = max(1, int(block_size))
        region = target.crop((x1, y1, x2, y2))
        try:
            small = region.resize(
                (max(1, rw // step), max(1, rh // step)),
                Image.Resampling.BOX,
            )
            try:
                mosaic = small.resize((rw, rh), Image.Resampling.NEAREST)
                try:
                    target.paste(mosaic, (x1, y1))
                finally:
                    mosaic.close()
            finally:
                small.close()
        finally:
            region.close()

    @staticmethod
    def apply_mosaic(img: Image.Image, bbox: tuple, block_size: int = 15) -> Image.Image:
        """对图片指定区域应用马赛克效果。

        Args:
            img: PIL Image 对象
            bbox: 边界框 (x1, y1, x2, y2)，支持相对坐标(0-1)或绝对坐标
            block_size: 马赛克块大小（像素）

        Returns:
            处理后的 PIL Image 对象
        """
        result = img.convert('RGB') if img.mode != 'RGB' else img.copy()
        ImageUtils._apply_mosaic_in_place(result, bbox, block_size)
        return result

    @staticmethod
    def apply_mosaic_multi(img: Image.Image, bboxes: list, block_size: int = 15) -> Image.Image:
        """对多个区域应用马赛克效果（仅复制一次底图，避免逐框反复拷图）。

        Args:
            img: PIL Image 对象
            bboxes: 边界框列表 [(x1, y1, x2, y2), ...]
            block_size: 马赛克块大小

        Returns:
            处理后的 PIL Image 对象
        """
        valid_bboxes = [
            bbox[:4]
            for bbox in bboxes
            if isinstance(bbox, (list, tuple)) and len(bbox) >= 4
        ]
        if not valid_bboxes:
            return img
        result = img.convert('RGB') if img.mode != 'RGB' else img.copy()
        for bbox in valid_bboxes:
            ImageUtils._apply_mosaic_in_place(result, bbox, block_size)
        return result
