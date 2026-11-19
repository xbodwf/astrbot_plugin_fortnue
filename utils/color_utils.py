"""颜色处理工具模块"""
import random


class ColorUtils:
    """颜色处理工具类"""

    @staticmethod
    def get_color_name(rgb: tuple) -> str:
        """根据RGB值范围获取中文颜色名"""
        r, g, b = rgb

        # 计算亮度与灰度
        brightness = (r + g + b) / 3
        max_val = max(r, g, b)
        min_val = min(r, g, b)
        delta = max_val - min_val

        if brightness < 30:
            return "黑色"
        if brightness > 240 and delta < 30:
            return "白色"
        if delta < 20:
            if brightness > 190:
                return "浅灰色"
            if brightness < 90:
                return "深灰色"
            return "灰色"

        # 计算色相 (0-360) 与饱和度 (0-100)
        if max_val == r:
            h = 60 * (((g - b) / delta) % 6)
        elif max_val == g:
            h = 60 * (((b - r) / delta) + 2)
        else:
            h = 60 * (((r - g) / delta) + 4)
        h %= 360

        s = (delta / max_val * 100) if max_val else 0
        if s < 20:
            return "浅灰色" if brightness > 180 else "灰色"

        if h < 15 or h >= 345:
            if s > 70 and r > 200 and g < 100 and b < 100:
                return "正红色"
            if r > 200 and g > 140 and b > 140:
                return "粉红色"
            base = "红色"
        elif h < 45:
            base = "棕色" if max_val < 140 else "橙色"
        elif h < 75:
            base = "黄色"
        elif h < 165:
            base = "绿色"
        elif h < 195:
            base = "青色"
        elif h < 215 and brightness > 150:
            return "天蓝色"
        elif h < 255:
            base = "蓝色"
        elif h < 285:
            base = "紫色"
        else:
            base = "洋红" if brightness < 180 else "粉色"

        if max_val < 110:
            return f"深{base}"
        if brightness > 200 and s < 50:
            return f"浅{base}"
        return base

    @staticmethod
    def get_random_hex_color() -> dict:
        """生成随机的十六进制颜色"""
        r = random.randint(0, 255)
        g = random.randint(0, 255)
        b = random.randint(0, 255)
        hex_color = f"#{r:02x}{g:02x}{b:02x}".upper()

        return {
            "name": ColorUtils.get_color_name((r, g, b)),
            "hex": hex_color,
            "rgb": [r, g, b],
        }
