import os
from dotenv import load_dotenv

load_dotenv()

# 知乎 Cookie（可选，不提供则使用公开接口）
COOKIE = os.getenv("ZHIHU_COOKIE", "")

# 请求间隔（秒），随机范围
REQUEST_DELAY_MIN = float(os.getenv("REQUEST_DELAY_MIN", "1"))
REQUEST_DELAY_MAX = float(os.getenv("REQUEST_DELAY_MAX", "2"))

# 浏览器接口请求间隔（秒）
BROWSER_DELAY_MIN = float(os.getenv("BROWSER_DELAY_MIN", "0.4"))
BROWSER_DELAY_MAX = float(os.getenv("BROWSER_DELAY_MAX", "0.8"))

# 保守模式下的请求间隔（秒）
CONSERVATIVE_REQUEST_DELAY_MIN = float(os.getenv("CONSERVATIVE_REQUEST_DELAY_MIN", "2.0"))
CONSERVATIVE_REQUEST_DELAY_MAX = float(os.getenv("CONSERVATIVE_REQUEST_DELAY_MAX", "4.0"))
CONSERVATIVE_BROWSER_DELAY_MIN = float(os.getenv("CONSERVATIVE_BROWSER_DELAY_MIN", "1.2"))
CONSERVATIVE_BROWSER_DELAY_MAX = float(os.getenv("CONSERVATIVE_BROWSER_DELAY_MAX", "2.4"))

# 导出 HTML 时图片下载间隔（秒）
ASSET_DOWNLOAD_DELAY_MIN = float(os.getenv("ASSET_DOWNLOAD_DELAY_MIN", "0.15"))
ASSET_DOWNLOAD_DELAY_MAX = float(os.getenv("ASSET_DOWNLOAD_DELAY_MAX", "0.35"))
CONSERVATIVE_ASSET_DOWNLOAD_DELAY_MIN = float(os.getenv("CONSERVATIVE_ASSET_DOWNLOAD_DELAY_MIN", "0.6"))
CONSERVATIVE_ASSET_DOWNLOAD_DELAY_MAX = float(os.getenv("CONSERVATIVE_ASSET_DOWNLOAD_DELAY_MAX", "1.2"))

# 失败重试次数
MAX_RETRIES = 3

# 单次请求超时（秒）
REQUEST_TIMEOUT = float(os.getenv("REQUEST_TIMEOUT", "30"))

# 问题回答分批保存大小
QUESTION_BATCH_SIZE = int(os.getenv("QUESTION_BATCH_SIZE", "50"))

# 输出目录
OUTPUT_DIR = os.path.join(os.path.dirname(__file__), "output")
