"""
【学习标注：旧转换引擎】
数据格式转换核心模块  (converter_core.py)
=============================================
纯内存转换引擎，不依赖 HTTP / 文件系统。新平台的安全入口是
backend/app/tasks.py::run_job，它负责权限、任务状态、配额、血缘和审计后再调用 convert()。
建议按 detect_type → convert → 各 _convert_* 实现的顺序阅读。

**使用方式** ::

    from converter_core import convert, detect_type, get_formats, FileType

    # 1. 从数据平台拉取数据集（你的 API 层负责）
    raw_bytes: bytes = fetch_from_warehouse("SELECT * FROM orders")

    # 2. 转换为目标格式
    result = convert(raw_bytes, source_format="csv", target_format="json")
    # result.success  -> True
    # result.content  -> b'[{"id":1,...}]'
    # result.format   -> "json"

    # 3. 写回数据平台
    upload_to_warehouse(result.content)


支持的类型
----------
- 图像: jpg, png, webp, bmp, gif, tiff, ico, heic, avif, svg
- 视频/音频: mp4, webm, avi, mkv, mov, mp3, wav, aac, ogg, flac...
- 数据: csv, xlsx, json, xml, html, txt
- 文档: pdf, docx, txt, html, md, rtf
"""

from __future__ import annotations

import io
import os
import re
import sys
import tempfile
import shutil
import subprocess
import asyncio
import json
import base64
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from enum import StrEnum
from dataclasses import dataclass, field
from typing import Callable, ClassVar, Any, Protocol

# ============================================================================
# SECTION A — 公共基础设施
# ============================================================================

# ── 枚举 ────────────────────────────────────────────────────────────────────

class FileType(StrEnum):
    """根据扩展名检测到的文件类别。"""
    IMAGE = "image"
    VIDEO = "video"
    AUDIO = "audio"
    PDF = "pdf"
    DOCX = "docx"
    DATA = "data"
    UNKNOWN = "unknown"


# ── 数据类 ─────────────────────────────────────────────────────────────────

@dataclass
class ConvertResult:
    """转换结果（纯内存，无文件路径）。"""
    success: bool
    content: bytes | None = None        # 转换后的数据内容
    format: str | None = None           # 实际输出格式（扩展名，不含点）
    source_format: str | None = None    # 输入格式
    error: str | None = None
    meta: dict | None = field(default_factory=dict)  # 附加信息（行数、多文件等）


@dataclass
class ConvertConfig:
    """可选转换参数。"""
    timeout_seconds: int = 300
    quality: int = 95                     # 图像/视频质量（1-100）
    max_size_bytes: int | None = None     # 目标文件大小上限（字节），超过则自动压缩
    target_dimensions: tuple[int, int] | None = None  # 图像统一尺寸（宽, 高），如 (24,24) 或 (1920,1080)。大图缩小、小图放大
    csv_delimiter: str | None = None      # CSV 分隔符（自动检测）
    sheet_name: str | int = 0             # Excel 目标 sheet
    pdf_page_range: tuple[int, int] | None = None  # PDF 页码范围


@dataclass
class FileItem:
    """数据集中的单个文件（上游返回多文件时使用）。

    用于替代原来把多个文件拼成一个 bytes 的做法，
    支持混合格式数据集——按 source_format 分组后异步转换。
    """
    name: str                          # 文件名/标识（如 "sales_20260701.csv"）
    data: bytes                        # 原始数据内容
    source_format: str                 # 源格式（扩展名，如 "csv", "pdf"）
    row_id: int | None = None          # MySQL 行 ID（可选，用于审计追踪）


# ── 常量映射表 ─────────────────────────────────────────────────────────────

# 扩展名 → FileType
EXTENSION_MAP: dict[str, FileType] = {
    # 图像
    '.jpg': FileType.IMAGE, '.jpeg': FileType.IMAGE, '.png': FileType.IMAGE,
    '.webp': FileType.IMAGE, '.bmp': FileType.IMAGE, '.tiff': FileType.IMAGE,
    '.tif': FileType.IMAGE, '.ico': FileType.IMAGE, '.gif': FileType.IMAGE,
    '.heic': FileType.IMAGE, '.heif': FileType.IMAGE, '.svg': FileType.IMAGE,
    '.avif': FileType.IMAGE,
    # 视频
    '.mp4': FileType.VIDEO, '.mov': FileType.VIDEO, '.avi': FileType.VIDEO,
    '.mkv': FileType.VIDEO, '.webm': FileType.VIDEO, '.flv': FileType.VIDEO,
    '.wmv': FileType.VIDEO, '.m4v': FileType.VIDEO, '.3gp': FileType.VIDEO,
    '.mpeg': FileType.VIDEO, '.mpg': FileType.VIDEO, '.ts': FileType.VIDEO,
    # 音频
    '.mp3': FileType.AUDIO, '.wav': FileType.AUDIO, '.flac': FileType.AUDIO,
    '.aac': FileType.AUDIO, '.ogg': FileType.AUDIO, '.m4a': FileType.AUDIO,
    '.wma': FileType.AUDIO, '.aiff': FileType.AUDIO, '.opus': FileType.AUDIO,
    '.ac3': FileType.AUDIO, '.amr': FileType.AUDIO, '.m4r': FileType.AUDIO,
    # 文档
    '.csv': FileType.DATA, '.xlsx': FileType.DATA, '.xls': FileType.DATA,
    '.json': FileType.DATA, '.xml': FileType.DATA,
    '.html': FileType.DATA, '.txt': FileType.DATA,
    '.pdf': FileType.PDF,
    '.docx': FileType.DOCX, '.doc': FileType.DOCX,
}

# FileType → 可转换的目标格式列表
FORMAT_OPTIONS: dict[FileType, list[str]] = {
    FileType.IMAGE:  ['webp', 'png', 'jpg', 'gif', 'bmp', 'tiff', 'ico', 'pdf'],
    FileType.VIDEO:  ['mp4', 'webm', 'avi', 'mkv', 'mov', 'gif', 'mp3', 'wav'],
    FileType.AUDIO:  ['mp3', 'wav', 'aac', 'ogg', 'flac', 'm4a'],
    FileType.PDF:    ['docx', 'txt', 'html', 'md', 'png', 'jpg'],
    FileType.DOCX:   ['pdf', 'txt', 'html', 'md'],
    FileType.DATA:   ['csv', 'xlsx', 'json', 'xml', 'html', 'txt'],
    FileType.UNKNOWN: [],
}


# 视频格式列表（用于判断是否需要视频编码）
_VIDEO_FORMATS = {'mp4', 'webm', 'avi', 'mkv', 'mov', 'wmv', 'flv', 'm4v', '3gp', 'mpeg', 'mpg', 'ts', 'gif'}
_AUDIO_FORMATS = {'mp3', 'wav', 'aac', 'ogg', 'flac', 'm4a', 'wma', 'aiff', 'opus', 'ac3', 'amr', 'm4r'}

# ── 工具函数 ───────────────────────────────────────────────────────────────

def detect_type(filename_or_ext: str) -> FileType:
    """从文件名或扩展名推断文件类型。

    Args:
        filename_or_ext: 文件名（如 "data.csv"）或扩展名（如 ".csv" 或 "csv"）

    Returns:
        FileType 枚举值
    """
    if not filename_or_ext.startswith('.'):
        ext = os.path.splitext(filename_or_ext.lower())[1]
    else:
        ext = filename_or_ext.lower()
    return EXTENSION_MAP.get(ext, FileType.UNKNOWN)


def get_formats(file_type_or_ext: str | FileType) -> list[str]:
    """返回可以转换到的目标格式列表。

    Args:
        file_type_or_ext: FileType 或文件名/扩展名

    Returns:
        目标格式扩展名列表（不含点）
    """
    if isinstance(file_type_or_ext, FileType):
        ft = file_type_or_ext
    else:
        ft = detect_type(file_type_or_ext)
    return FORMAT_OPTIONS.get(ft, [])


def get_convertible_extensions(file_type: FileType) -> list[str]:
    """返回属于某类的所有输入扩展名。"""
    return [ext for ext, ft in EXTENSION_MAP.items() if ft == file_type]


def _validate_identifier(name: str, max_len: int = 128) -> str:
    """校验 SQL 标识符（表名、列名），防止注入。

    只允许字母、数字、下划线，且不以数字开头。"""
    if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]{0,' + str(max_len - 1) + '}', name):
        raise ValueError(f"非法标识符: {name!r}（只允许字母数字下划线，最长{max_len}字符）")
    return name


def _validate_flow_id(flow_id: str) -> str:
    """校验并清洗 flow_id，用于拼接表名。"""
    cleaned = flow_id.replace("-", "_")
    if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]{0,127}', cleaned):
        raise ValueError(f"非法的 flow_id: {flow_id!r}")
    return cleaned


def _find_ffmpeg() -> str:
    """查找 FFmpeg 可执行文件路径。

    优先级: 项目自带的 ffmpeg/ 目录 > 系统 PATH > 回退 "ffmpeg"
    """
    ffmpeg_dir = Path(__file__).resolve().parent / "ffmpeg"
    if ffmpeg_dir.exists():
        candidates = [
            ffmpeg_dir / "bin" / "ffmpeg.exe",
            ffmpeg_dir / "ffmpeg.exe",
        ]
        for p in candidates:
            if p.exists():
                return str(p)
        for item in ffmpeg_dir.iterdir():
            if item.is_dir():
                exe = item / "bin" / "ffmpeg.exe"
                if exe.exists():
                    return str(exe)
                exe = item / "ffmpeg.exe"
                if exe.exists():
                    return str(exe)
    system = shutil.which("ffmpeg")
    return system or "ffmpeg"


# 模块级 FFmpeg 路径缓存
_FFMPEG_PATH: str | None = None


def _get_ffmpeg() -> str:
    global _FFMPEG_PATH
    if _FFMPEG_PATH is None:
        _FFMPEG_PATH = _find_ffmpeg()
    return _FFMPEG_PATH


def check_ffmpeg_available() -> tuple[bool, str | None]:
    """检查 FFmpeg 是否可用。返回 (是否可用, 路径)。"""
    path = _get_ffmpeg()
    if path == "ffmpeg":
        # 尝试用 which 确认系统 PATH 中真的存在
        found = shutil.which("ffmpeg")
        if found:
            return True, found
        return False, None
    return True, path


# ============================================================================
# SECTION B — 核心调度
# ============================================================================

def convert(
    data: bytes,
    source_format: str,
    target_format: str,
    config: ConvertConfig | None = None,
) -> ConvertResult:
    """核心入口：将一种格式的数据转换为另一种格式。

    这是整个模块的唯一公共转换入口。调用方负责：
    1. 从数据平台拉取原始字节数据
    2. 调用 convert()
    3. 将结果写回或传给下游

    Args:
        data: 原始数据内容（字节）
        source_format: 源格式（扩展名，如 "csv", "xlsx", "json"）
        target_format: 目标格式（扩展名，如 "json", "csv", "xlsx"）
        config: 可选转换参数

    Returns:
        ConvertResult（包含转换后数据或错误信息）

    Example:
        >>> result = convert(b'name,age\\nAlice,30', source_format="csv", target_format="json")
        >>> result.success
        True
        >>> b'Alice' in result.content
        True
    """
    cfg = config or ConvertConfig()
    sf = source_format.lower().lstrip('.')
    tf = target_format.lower().lstrip('.')

    if sf == tf and not (cfg.max_size_bytes or cfg.target_dimensions):
        return ConvertResult(success=True, content=data, format=tf, source_format=sf)

    file_type = detect_type(f".{sf}")

    try:
        if file_type == FileType.IMAGE:
            return _convert_image(data, sf, tf, cfg)
        elif file_type in (FileType.VIDEO, FileType.AUDIO):
            return _convert_media(data, sf, tf, cfg)
        elif file_type == FileType.DATA:
            return _convert_data(data, sf, tf, cfg)
        elif file_type == FileType.PDF:
            return _convert_pdf(data, tf, cfg)
        elif file_type == FileType.DOCX:
            return _convert_docx(data, tf, cfg)
        else:
            return ConvertResult(
                success=False, error=f"不支持的源格式: {sf}",
                source_format=sf, format=tf,
            )
    except Exception as exc:
        return ConvertResult(
            success=False, error=f"转换异常: {exc}",
            source_format=sf, format=tf,
        )


def fit_size(
    data: bytes,
    source_format: str,
    max_size_bytes: int | None = None,
    target_dimensions: tuple[int, int] | None = None,
    timeout_seconds: int = 300,
) -> ConvertResult:
    """纯大小拟合——不改变格式，调整尺寸/大小到目标。

    与 convert() 独立——用户可以：
    - 只调文件大小: fit_size(data, "json", max_size_bytes=10000)
    - 只调图像尺寸: fit_size(data, "png", target_dimensions=(24, 24))
    - 同时调:     fit_size(data, "jpg", max_size_bytes=5000, target_dimensions=(128, 128))
    - 配合 convert: convert(data, "csv", "json", config=ConvertConfig(max_size_bytes=10000))

    Args:
        data: 已转换/已有的文件内容（字节）
        source_format: 当前格式（扩展名）
        max_size_bytes: 目标文件大小上限（字节）；None 则不限制
        target_dimensions: 图像统一尺寸（宽, 高），如 (24,24) 或 (1920,1080)。
                            大图缩小、小图放大。
        timeout_seconds: 媒体处理超时秒数

    Returns:
        ConvertResult（fitted=True/False 记录在 meta 中）
    """
    sf = source_format.lower().lstrip('.')

    # target_dimensions 需要强制缩放，即使字节数已经够小也不能跳过
    needs_resize = target_dimensions is not None
    if max_size_bytes and len(data) <= max_size_bytes and not needs_resize:
        return ConvertResult(
            success=True, content=data, format=sf, source_format=sf,
            meta={"fitted": True, "note": f"已在 {max_size_bytes} 字节以内，无需压缩"},
        )

    file_type = detect_type(f".{sf}")

    try:
        if file_type == FileType.IMAGE:
            return _fit_image_bytes(data, sf, max_size_bytes or 0, 95,
                                    target_dimensions)
        elif file_type in (FileType.VIDEO, FileType.AUDIO):
            if max_size_bytes and len(data) > max_size_bytes:
                return _fit_media_bytes(data, sf, max_size_bytes, 95, timeout_seconds)
            return ConvertResult(
                success=True, content=data, format=sf, source_format=sf,
                meta={"fitted": True, "note": "媒体文件已符合大小要求"},
            )
        elif file_type == FileType.DATA:
            if max_size_bytes and len(data) > max_size_bytes:
                return _fit_data_bytes(data, sf, max_size_bytes)
            return ConvertResult(
                success=True, content=data, format=sf, source_format=sf,
                meta={"fitted": True, "note": "数据文件已符合大小要求"},
            )
        else:
            return ConvertResult(
                success=True, content=data, format=sf, source_format=sf,
                meta={"fitted": False, "note": "此文件类型不支持大小拟合，返回原始数据"},
            )
    except Exception as exc:
        return ConvertResult(
            success=False, error=f"大小拟合异常: {exc}",
            source_format=sf, format=sf,
        )


# ============================================================================
# SECTION C — 各类型转换器（纯内存 I/O）
# ============================================================================

# ── C.1  图像转换器 ──────────────────────────────────────────────────────

# 延迟导入标记
_HAS_HEIF: bool | None = None
_HAS_AVIF: bool | None = None


def _check_heif() -> bool:
    global _HAS_HEIF
    if _HAS_HEIF is None:
        try:
            import pillow_heif
            pillow_heif.register_heif_opener()
            _HAS_HEIF = True
        except ImportError:
            _HAS_HEIF = False
    return _HAS_HEIF


def _check_avif() -> bool:
    global _HAS_AVIF
    if _HAS_AVIF is None:
        try:
            from PIL import features
            _HAS_AVIF = features.check('avif')
        except Exception:
            _HAS_AVIF = False
    return _HAS_AVIF


def _check_format_support(source_format: str, target_format: str) -> str | None:
    """检查格式组合是否支持。不支持返回错误信息，支持返回 None。

    在 MinIO 下载前调用，避免下载了大文件才发现转不了。
    """
    sf = source_format.lower().lstrip('.')
    tf = target_format.lower().lstrip('.')

    # HEIF/HEIC 输入/输出需要 pillow-heif
    if sf in ('heic', 'heif') or tf in ('heic', 'heif'):
        if not _check_heif():
            return f"HEIC/HEIF 格式需要 pillow-heif 库（pip install pillow-heif）"

    # AVIF 需要 Pillow 9.1+ 和系统 libavif
    if sf == 'avif' or tf == 'avif':
        if not _check_avif():
            return f"AVIF 格式需要 Pillow 9.1+ 和系统 libavif 库"

    return None


def _convert_image(data: bytes, source_format: str, target_format: str, config: ConvertConfig) -> ConvertResult:
    """图像转换：bytes → Pillow → bytes，支持目标大小限制。"""
    from PIL import Image

    try:
        input_buf = io.BytesIO(data)

        # SVG 特殊处理
        if source_format == 'svg':
            return _convert_svg_to_raster(data, target_format)

        with Image.open(input_buf) as img:
            original_mode = img.mode

            # 转换为不支持透明度的目标格式
            if target_format in ('jpg', 'jpeg', 'bmp', 'pdf') and original_mode in ('RGBA', 'P', 'LA'):
                background = Image.new('RGB', img.size, (255, 255, 255))
                if img.mode == 'P':
                    img = img.convert('RGBA')
                if img.mode in ('RGBA', 'LA'):
                    background.paste(img, mask=img.split()[-1])
                else:
                    img = img.convert('RGB')
                img = background

            # 图像尺寸控制
            if config.target_dimensions:
                img = img.resize(config.target_dimensions, Image.LANCZOS)

            # 初次保存
            content = _save_image(img, target_format, config.quality)

            # 如果设置了大小上限且超标，自动压缩
            if config.max_size_bytes and len(content) > config.max_size_bytes:
                content = _fit_image_size(img, target_format, config)

        return ConvertResult(
            success=True, content=content,
            format=target_format, source_format=source_format,
        )

    except Exception as e:
        return ConvertResult(success=False, error=f"图像转换错误：{e}",
                             source_format=source_format, format=target_format)


def _save_image(img, target_format: str, quality: int) -> bytes:
    """保存图像到字节缓冲区（提取为独立函数，供大小拟合循环复用）。"""
    from PIL import Image

    buf = io.BytesIO()

    if target_format in ('jpg', 'jpeg'):
        # subsampling=0 + optimize=True 组合在某些图像上会触发 JPEG 编码器 bug
        # 优先使用完整选项，失败则逐级回退
        for opt, sub in [(True, 0), (True, None), (False, None)]:
            try:
                buf.seek(0); buf.truncate()
                kwargs = {'quality': quality}
                if sub is not None:
                    kwargs['subsampling'] = sub
                if opt:
                    kwargs['optimize'] = True
                img.save(buf, 'JPEG', **kwargs)
                return buf.getvalue()
            except OSError:
                continue
        # 最终兜底：不做任何优化
        buf.seek(0); buf.truncate()
        img.save(buf, 'JPEG', quality=quality)
        return buf.getvalue()
    elif target_format == 'png':
        img.save(buf, 'PNG', optimize=True)
    elif target_format == 'webp':
        kwargs: dict = {'quality': quality, 'method': 6}
        if getattr(img, 'n_frames', 1) > 1:
            kwargs['save_all'] = True
        img.save(buf, 'WEBP', **kwargs)
    elif target_format == 'gif':
        if img.mode != 'P':
            img = img.convert('P', palette=Image.ADAPTIVE, colors=256)
        gif_kwargs: dict = {'optimize': True}
        if getattr(img, 'n_frames', 1) > 1:
            gif_kwargs['save_all'] = True
            gif_kwargs['loop'] = 0
        img.save(buf, 'GIF', **gif_kwargs)
    elif target_format == 'bmp':
        img.save(buf, 'BMP')
    elif target_format in ('tiff', 'tif'):
        img.save(buf, 'TIFF', compression='tiff_lzw')
    elif target_format == 'ico':
        sizes = [(256, 256), (128, 128), (64, 64), (48, 48), (32, 32), (16, 16)]
        img_copy = img.copy()
        img_copy.thumbnail((256, 256), Image.LANCZOS)
        img_copy.save(buf, 'ICO', sizes=sizes)
    elif target_format == 'pdf':
        if img.mode != 'RGB':
            img = img.convert('RGB')
        img.save(buf, 'PDF', resolution=150.0)
    elif target_format in ('heic', 'heif'):
        if not _check_heif():
            raise RuntimeError("HEIC 格式暂不支持，请安装 pillow-heif")
        if img.mode != 'RGB':
            img = img.convert('RGB')
        img.save(buf, 'HEIF', quality=quality)
    elif target_format == 'avif':
        if not _check_avif():
            raise RuntimeError("AVIF 支持需要 Pillow 9.1+ 和 libavif")
        img.save(buf, 'AVIF', quality=quality)
    else:
        img.save(buf)

    return buf.getvalue()


def _fit_image_size(img, target_format: str, config: ConvertConfig) -> bytes:
    """二分搜索质量/尺寸，使图像文件大小 ≤ max_size_bytes。

    策略（按优先级）：
    1. 有损格式（JPEG/WEBP/AVIF/HEIC）：二分搜索 quality ∈ [5, 95]
    2. PNG：先尝试颜色量化，再逐步缩小尺寸
    3. 其他无损格式：等比缩小尺寸
    """
    from PIL import Image

    max_bytes = config.max_size_bytes
    if not max_bytes:
        return _save_image(img.copy(), target_format, config.quality)

    _LOSSLESS = {'png', 'bmp', 'tiff', 'tif', 'ico', 'gif'}

    # ── 策略 1：有损格式 — 二分搜索质量 ──
    if target_format not in _LOSSLESS and target_format != 'pdf':
        lo, hi = 5, config.quality
        # 每次保存前复制图像，避免内部状态污染
        best = _save_image(img.copy(), target_format, 5)
        for _ in range(12):
            mid = (lo + hi) // 2
            content = _save_image(img.copy(), target_format, mid)
            if len(content) <= max_bytes:
                best = content
                lo = mid + 1
            else:
                hi = mid - 1
            if lo > hi:
                break
        return best

    # ── 策略 2：PNG — 先量化颜色，再缩尺寸 ──
    if target_format == 'png':
        # 2a. 颜色量化（从 256 色降到 16 色）
        for colors in [256, 128, 64, 32, 16, 8]:
            try:
                quantized = img.copy().convert('P', palette=Image.ADAPTIVE, colors=colors)
                content = _save_image(quantized, target_format, config.quality)
                if len(content) <= max_bytes:
                    return content
            except Exception:
                pass

    # ── 策略 3：逐步缩小尺寸 ──
    w, h = img.size
    for scale in [0.75, 0.5, 0.35, 0.25, 0.18, 0.12, 0.08, 0.05, 0.03]:
        nw, nh = max(16, int(w * scale)), max(16, int(h * scale))
        resized = img.copy().resize((nw, nh), Image.LANCZOS)
        content = _save_image(resized, target_format, config.quality)
        if len(content) <= max_bytes:
            return content
    # 最终兜底
    final = img.copy().resize((16, 16), Image.LANCZOS)
    return _save_image(final, target_format, config.quality)


def _fit_image_bytes(data: bytes, source_format: str, max_bytes: int,
                     quality: int,
                     target_dimensions: tuple[int, int] | None = None) -> ConvertResult:
    """fit_size() 的图像分支：bytes → PIL → 尺寸调整 → 大小拟合 → bytes。

    处理顺序：
    1. target_dimensions: 强制缩放到指定宽高（大图缩小、小图放大）
    2. max_bytes:          二分搜索质量/尺寸使文件 ≤ 目标字节数
    """
    from PIL import Image

    try:
        img = Image.open(io.BytesIO(data))
        original_size = img.size

        # Step 1: 强制统一尺寸（大图缩小，小图放大）
        if target_dimensions:
            img = img.resize(target_dimensions, Image.LANCZOS)

        # 文件大小拟合
        if max_bytes > 0:
            cfg = ConvertConfig(quality=quality, max_size_bytes=max_bytes)
            content = _fit_image_size(img, source_format, cfg)
        else:
            content = _save_image(img, source_format, quality)

        fitted = (max_bytes <= 0 or len(content) <= max_bytes)
        meta_parts = []
        if target_dimensions and original_size != target_dimensions:
            tw, th = target_dimensions
            meta_parts.append(f"尺寸 {original_size[0]}x{original_size[1]} → {tw}x{th}")
        if max_bytes > 0:
            meta_parts.append(f"{'已压缩' if fitted else '无法压缩'}到 {max_bytes} bytes（实际 {len(content)} bytes）")

        return ConvertResult(
            success=True, content=content,
            format=source_format, source_format=source_format,
            meta={
                "fitted": fitted,
                "original_size": list(original_size),
                "final_size": [img.size[0], img.size[1]],
                "target_dimension_applied": target_dimensions is not None,
                "note": "; ".join(meta_parts) if meta_parts else "无需调整",
            },
        )
    except Exception as e:
        return ConvertResult(success=False, error=f"图像大小拟合错误：{e}",
                           source_format=source_format, format=source_format)


def _convert_svg_to_raster(data: bytes, target_format: str) -> ConvertResult:
    """SVG → 栅格格式（通过 cairosvg）。"""
    try:
        import cairosvg
    except ImportError:
        return ConvertResult(success=False, error="SVG 转换需要安装 cairosvg")

    svg_str = data.decode('utf-8')

    try:
        if target_format == 'png':
            content = cairosvg.svg2png(bytestring=svg_str, scale=2.0)
        elif target_format == 'pdf':
            content = cairosvg.svg2pdf(bytestring=svg_str)
        elif target_format in ('jpg', 'jpeg'):
            png_data = cairosvg.svg2png(bytestring=svg_str, scale=2.0)
            from PIL import Image
            with Image.open(io.BytesIO(png_data)) as img:
                buf = io.BytesIO()
                img.convert('RGB').save(buf, 'JPEG', quality=95)
                content = buf.getvalue()
        else:
            return ConvertResult(success=False, error=f"SVG 只能转换为 PNG、PDF 或 JPG")
        return ConvertResult(success=True, content=content, format=target_format)
    except Exception as e:
        return ConvertResult(success=False, error=f"SVG 转换错误：{e}")


# ── C.2  媒体转换器（视频/音频）───────────────────────────────────────────

def _convert_media(data: bytes, source_format: str, target_format: str, config: ConvertConfig) -> ConvertResult:
    """媒体转换：bytes → 临时文件 → FFmpeg → bytes。

    使用 tempfile 的原因是 FFmpeg 必须通过文件路径操作。
    大小控制：音频二分搜索比特率，视频二分搜索 CRF，不再用 -fs 硬截断。
    """
    ffmpeg_cmd = _get_ffmpeg()

    tmp_dir = tempfile.mkdtemp(prefix="mediaconv_")
    try:
        input_path = os.path.join(tmp_dir, f"input.{source_format}")
        output_path = os.path.join(tmp_dir, f"output.{target_format}")

        with open(input_path, 'wb') as f:
            f.write(data)

        # 初次转换（高质量）
        cmd = [ffmpeg_cmd, '-y', '-i', input_path]
        cmd.extend(_get_ffmpeg_options(target_format))
        cmd.append(output_path)

        try:
            rc, stdout, stderr = _run_ffmpeg(cmd, output_path, config.timeout_seconds)
            if rc != 0:
                raise subprocess.CalledProcessError(rc, cmd, stdout, stderr)
        except subprocess.TimeoutError:
            return ConvertResult(
                success=False, error=f"媒体转换超时（{config.timeout_seconds}秒）",
                source_format=source_format, format=target_format,
            )

        if not os.path.exists(output_path):
            return ConvertResult(
                success=False, error="FFmpeg 转换失败，未生成输出文件",
                source_format=source_format, format=target_format,
            )

        # 大小控制：二分搜索比特率/CRF（不再用 -fs 截断）
        if config.max_size_bytes:
            size = os.path.getsize(output_path)
            if size > config.max_size_bytes:
                content, fitted = _fit_media_size(
                    input_path, output_path, target_format,
                    config.max_size_bytes, ffmpeg_cmd, config.timeout_seconds,
                )
                if fitted:
                    return ConvertResult(
                        success=True, content=content,
                        format=target_format, source_format=source_format,
                        meta={"fitted": True, "note": f"已自动压缩以符合 {config.max_size_bytes} 字节上限"},
                    )
                # 拟合失败，返回已达到的最小体积
                return ConvertResult(
                    success=True, content=content,
                    format=target_format, source_format=source_format,
                    meta={"fitted": False, "note": f"无法压缩到 {config.max_size_bytes} 字节，返回最小可能体积"},
                )

        with open(output_path, 'rb') as f:
            content = f.read()
        return ConvertResult(
            success=True, content=content,
            format=target_format, source_format=source_format,
        )

    except Exception as e:
        return ConvertResult(
            success=False, error=f"媒体转换错误: {e}",
            source_format=source_format, format=target_format,
        )
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def _fit_media_size(input_path: str, output_path: str, target_format: str,
                    max_bytes: int, ffmpeg_cmd: str, timeout: int) -> tuple[bytes, bool]:
    """二分搜索音频比特率或视频 CRF，使输出 ≤ max_bytes。

    策略：
    ┌──────────────┬──────────────────────┬──────────────────┐
    │ 格式类型       │ 搜索参数              │ 范围              │
    ├──────────────┼──────────────────────┼──────────────────┤
    │ 有损音频       │ 比特率 -ab           │ 32k ~ 384k       │
    │ (mp3/aac/...) │                      │                  │
    ├──────────────┼──────────────────────┼──────────────────┤
    │ Vorbis 音频    │ 质量 -aq            │ 1 ~ 8            │
    │ (ogg)         │                      │                  │
    ├──────────────┼──────────────────────┼──────────────────┤
    │ 无损音频       │ 无法压缩              │ 保留 -fs 兜底     │
    │ (wav/flac/...)│                      │                  │
    ├──────────────┼──────────────────────┼──────────────────┤
    │ 视频(CRF)     │ CRF 值               │ 18 ~ 40          │
    │ (mp4/mkv/...) │                      │ (越高越压缩)      │
    ├──────────────┼──────────────────────┼──────────────────┤
    │ 视频(无CRF)   │ 视频比特率 -b:v       │ 256k ~ 4M        │
    └──────────────┴──────────────────────┴──────────────────┘

    Returns:
        (content_bytes, fitted: bool)  fitted=False 表示目标不可达
    """
    if target_format in _AUDIO_FORMATS:
        return _fit_audio(input_path, output_path, target_format,
                          max_bytes, ffmpeg_cmd, timeout)
    else:
        return _fit_video(input_path, output_path, target_format,
                          max_bytes, ffmpeg_cmd, timeout)


def _fit_audio(input_path, output_path, target_format, max_bytes, ffmpeg_cmd, timeout):
    """二分搜索音频比特率/质量。"""
    import shutil

    # 无损格式无法压缩，-fs 兜底
    _LOSSLESS_AUDIO = {'wav', 'flac', 'aiff'}
    if target_format in _LOSSLESS_AUDIO:
        try:
            cmd = [ffmpeg_cmd, '-y', '-i', input_path,
                   '-fs', str(max_bytes), output_path]
            _run_ffmpeg(cmd, output_path, timeout)
            if os.path.exists(output_path):
                with open(output_path, 'rb') as f:
                    return f.read(), os.path.getsize(output_path) <= max_bytes
        except Exception:
            pass
        with open(output_path, 'rb') as f:
            return f.read(), False

    # Vorbis 用 -aq (质量 1-8)
    if target_format == 'ogg':
        lo, hi = 1, 8
        best = None
        for _ in range(12):
            mid = (lo + hi) // 2
            opts = _get_ffmpeg_options(target_format, ogg_quality=mid)
            cmd = [ffmpeg_cmd, '-y', '-i', input_path] + opts + [output_path]
            _run_ffmpeg(cmd, output_path, timeout)
            if os.path.exists(output_path):
                sz = os.path.getsize(output_path)
                if sz <= max_bytes:
                    best = sz
                    lo = mid + 1
                else:
                    hi = mid - 1
            if lo > hi:
                break
        if best is not None:
            with open(output_path, 'rb') as f:
                return f.read(), True
        # 降到最低质量
        opts = _get_ffmpeg_options(target_format, ogg_quality=1)
        cmd = [ffmpeg_cmd, '-y', '-i', input_path] + opts + [output_path]
        _run_ffmpeg(cmd, output_path, timeout)
        with open(output_path, 'rb') as f:
            content = f.read()
        return content, len(content) <= max_bytes

    # 有损格式：二分搜索比特率
    # 默认比特率映射（作为搜索上限）
    default_br = {'mp3': 320, 'aac': 256, 'm4a': 256, 'opus': 192,
                  'ac3': 384, 'wma': 192, 'm4r': 128}
    hi = default_br.get(target_format, 256)
    lo = 32

    best_content = None
    for _ in range(12):
        mid = (lo + hi) // 2
        opts = _get_ffmpeg_options(target_format, bitrate_k=mid)
        cmd = [ffmpeg_cmd, '-y', '-i', input_path] + opts + [output_path]
        _run_ffmpeg(cmd, output_path, timeout)
        if os.path.exists(output_path):
            sz = os.path.getsize(output_path)
            if sz <= max_bytes:
                with open(output_path, 'rb') as f:
                    best_content = f.read()
                lo = mid + 1
            else:
                hi = mid - 1
        if lo > hi:
            break

    if best_content is not None:
        return best_content, True

    # 降到最低比特率
    opts = _get_ffmpeg_options(target_format, bitrate_k=lo)
    cmd = [ffmpeg_cmd, '-y', '-i', input_path] + opts + [output_path]
    _run_ffmpeg(cmd, output_path, timeout)
    with open(output_path, 'rb') as f:
        content = f.read()
    return content, len(content) <= max_bytes


def _fit_video(input_path, output_path, target_format, max_bytes, ffmpeg_cmd, timeout):
    """二分搜索视频 CRF 或比特率。"""
    # 有 CRF 参数的格式
    _HAS_CRF = {'mp4', 'mkv', 'mov', 'webm'}

    if target_format in _HAS_CRF:
        lo, hi = 18, 40  # CRF: 低=高质量, 高=高压缩
        best_content = None
        for _ in range(12):
            mid = (lo + hi) // 2
            opts = _get_ffmpeg_options(target_format, crf=mid)
            cmd = [ffmpeg_cmd, '-y', '-i', input_path] + opts + [output_path]
            _run_ffmpeg(cmd, output_path, timeout)
            if os.path.exists(output_path):
                sz = os.path.getsize(output_path)
                if sz <= max_bytes:
                    with open(output_path, 'rb') as f:
                        best_content = f.read()
                    hi = mid - 1  # 更高质量（更低 CRF）
                else:
                    lo = mid + 1  # 需要更多压缩（更高 CRF）
            if lo > hi:
                break

        if best_content is not None:
            return best_content, True
        # 降到最高压缩
        opts = _get_ffmpeg_options(target_format, crf=40)
        cmd = [ffmpeg_cmd, '-y', '-i', input_path] + opts + [output_path]
        _run_ffmpeg(cmd, output_path, timeout)
        with open(output_path, 'rb') as f:
            content = f.read()
        return content, len(content) <= max_bytes

    # 无 CRF 的视频格式：二分搜索 -b:v
    lo, hi = 256, 4000  # kbps
    best_content = None
    for _ in range(12):
        mid = (lo + hi) // 2
        opts = _get_ffmpeg_options(target_format, video_bitrate_k=mid)
        cmd = [ffmpeg_cmd, '-y', '-i', input_path] + opts + [output_path]
        _run_ffmpeg(cmd, output_path, timeout)
        if os.path.exists(output_path):
            sz = os.path.getsize(output_path)
            if sz <= max_bytes:
                with open(output_path, 'rb') as f:
                    best_content = f.read()
                lo = mid + 1
            else:
                hi = mid - 1
        if lo > hi:
            break

    if best_content is not None:
        return best_content, True
    # 降到最低
    opts = _get_ffmpeg_options(target_format, video_bitrate_k=256)
    cmd = [ffmpeg_cmd, '-y', '-i', input_path] + opts + [output_path]
    _run_ffmpeg(cmd, output_path, timeout)
    with open(output_path, 'rb') as f:
        content = f.read()
    return content, len(content) <= max_bytes


def _fit_media_bytes(data: bytes, source_format: str, max_bytes: int,
                     quality: int, timeout: int) -> ConvertResult:
    """fit_size() 的媒体分支：bytes → tempfile → _fit_media_size → bytes。"""
    ffmpeg_cmd = _get_ffmpeg()

    tmp_dir = tempfile.mkdtemp(prefix="fitsize_")
    try:
        input_path = os.path.join(tmp_dir, f"input.{source_format}")
        output_path = os.path.join(tmp_dir, f"output.{source_format}")

        with open(input_path, 'wb') as f:
            f.write(data)

        content, fitted = _fit_media_size(
            input_path, output_path, source_format,
            max_bytes, ffmpeg_cmd, timeout,
        )
        return ConvertResult(
            success=True, content=content,
            format=source_format, source_format=source_format,
            meta={"fitted": fitted, "note":
                  f"已压缩到 {len(content)} bytes" if fitted else
                  f"无法压缩到 {max_bytes} bytes，返回最小可能体积"},
        )
    except Exception as e:
        return ConvertResult(success=False, error=f"媒体大小拟合错误: {e}",
                           source_format=source_format, format=source_format)
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def _get_ffmpeg_options(target_format: str, bitrate_k: int | None = None,
                        crf: int | None = None, video_bitrate_k: int | None = None,
                        ogg_quality: int | None = None) -> list[str]:
    """获取 FFmpeg 编码参数。

    不传可选参数 = 使用高质量默认值。
    传入可选参数 = 用于二分搜索拟合大小。
    """
    opts: list[str] = []

    if target_format in _AUDIO_FORMATS:
        opts.append('-vn')
        if bitrate_k is not None:
            # 按指定比特率构造参数
            codec_map = {
                'mp3':  ['-acodec', 'libmp3lame', '-ab', f'{bitrate_k}k'],
                'aac':  ['-acodec', 'aac', '-ab', f'{bitrate_k}k'],
                'm4a':  ['-acodec', 'aac', '-ab', f'{bitrate_k}k'],
                'opus': ['-acodec', 'libopus', '-ab', f'{bitrate_k}k'],
                'ac3':  ['-acodec', 'ac3', '-ab', f'{bitrate_k}k'],
                'wma':  ['-acodec', 'wmav2', '-ab', f'{bitrate_k}k'],
            }
            opts.extend(codec_map.get(target_format, []))
        elif ogg_quality is not None:
            opts.extend(['-acodec', 'libvorbis', '-aq', str(ogg_quality)])
        else:
            # 默认高质量
            default_map = {
                'mp3':  ['-acodec', 'libmp3lame', '-ab', '320k'],
                'wav':  ['-acodec', 'pcm_s16le', '-ar', '44100'],
                'aac':  ['-acodec', 'aac', '-ab', '256k'],
                'ogg':  ['-acodec', 'libvorbis', '-aq', '8'],
                'flac': ['-acodec', 'flac', '-compression_level', '8'],
                'm4a':  ['-acodec', 'aac', '-ab', '256k'],
                'opus': ['-acodec', 'libopus', '-ab', '192k'],
                'aiff': ['-acodec', 'pcm_s16be'],
                'ac3':  ['-acodec', 'ac3', '-ab', '384k'],
                'wma':  ['-acodec', 'wmav2', '-ab', '192k'],
                'm4r':  ['-acodec', 'aac', '-ab', '128k', '-t', '30'],
            }
            opts.extend(default_map.get(target_format, []))

    elif target_format == 'gif':
        opts.extend([
            '-vf', 'fps=10,scale=480:-1:flags=lanczos,split[s0][s1];[s0]palettegen[p];[s1][p]paletteuse',
            '-loop', '0',
        ])

    elif target_format in _VIDEO_FORMATS:
        if crf is not None:
            crf_map = {
                'mp4':  ['-c:v', 'libx264', '-c:a', 'aac', '-movflags', '+faststart', '-crf', str(crf), '-preset', 'slow'],
                'webm': ['-c:v', 'libvpx-vp9', '-c:a', 'libopus', '-crf', str(crf)],
                'mkv':  ['-c:v', 'libx264', '-c:a', 'aac', '-crf', str(crf), '-preset', 'slow'],
                'mov':  ['-c:v', 'libx264', '-c:a', 'aac', '-tag:v', 'avc1', '-crf', str(crf)],
            }
            opts.extend(crf_map.get(target_format, []))
        elif video_bitrate_k is not None:
            br_map = {
                'mp4':  ['-c:v', 'libx264', '-c:a', 'aac', '-b:v', f'{video_bitrate_k}k', '-preset', 'slow'],
                'webm': ['-c:v', 'libvpx-vp9', '-c:a', 'libopus', '-b:v', f'{video_bitrate_k}k'],
                'mkv':  ['-c:v', 'libx264', '-c:a', 'aac', '-b:v', f'{video_bitrate_k}k', '-preset', 'slow'],
                'mov':  ['-c:v', 'libx264', '-c:a', 'aac', '-b:v', f'{video_bitrate_k}k'],
                'avi':  ['-c:v', 'mpeg4', '-c:a', 'mp3', '-b:v', f'{video_bitrate_k}k'],
                'wmv':  ['-c:v', 'wmv2', '-c:a', 'wmav2', '-b:v', f'{video_bitrate_k}k'],
                'flv':  ['-c:v', 'flv', '-c:a', 'mp3', '-b:v', f'{video_bitrate_k}k'],
                'm4v':  ['-c:v', 'libx264', '-c:a', 'aac', '-b:v', f'{video_bitrate_k}k'],
            }
            opts.extend(br_map.get(target_format, []))
        else:
            # 默认高质量
            default_video_map = {
                'mp4':  ['-c:v', 'libx264', '-c:a', 'aac', '-movflags', '+faststart', '-crf', '18', '-preset', 'slow'],
                'webm': ['-c:v', 'libvpx-vp9', '-c:a', 'libopus', '-b:v', '4M', '-crf', '20'],
                'avi':  ['-c:v', 'mpeg4', '-c:a', 'mp3', '-q:v', '3'],
                'mkv':  ['-c:v', 'libx264', '-c:a', 'aac', '-crf', '18', '-preset', 'slow'],
                'mov':  ['-c:v', 'libx264', '-c:a', 'aac', '-tag:v', 'avc1', '-crf', '18'],
                'wmv':  ['-c:v', 'wmv2', '-c:a', 'wmav2', '-b:v', '2M'],
                'flv':  ['-c:v', 'flv', '-c:a', 'mp3', '-ar', '44100'],
                'm4v':  ['-c:v', 'libx264', '-c:a', 'aac'],
                '3gp':  ['-c:v', 'h263', '-c:a', 'amr_nb', '-s', '352x288', '-ar', '8000'],
                'mpeg': ['-c:v', 'mpeg2video', '-c:a', 'mp2'],
                'mpg':  ['-c:v', 'mpeg2video', '-c:a', 'mp2'],
                'ts':   ['-c:v', 'libx264', '-c:a', 'aac', '-f', 'mpegts'],
            }
            opts.extend(default_video_map.get(target_format, []))

    return opts


# ── C.3  数据转换器（CSV / XLSX / JSON / XML / HTML / TXT）────────────────

def _convert_data(data: bytes, source_format: str, target_format: str, config: ConvertConfig) -> ConvertResult:
    """数据文件转换：bytes → pandas DataFrame → bytes。

    支持 CSV、XLSX、JSON、XML、HTML、TXT 之间的互转。
    """
    import pandas as pd

    try:
        df = _read_dataframe(data, source_format)
        if df is None:
            return ConvertResult(
                success=False, error="无法从数据中解析出 DataFrame",
                source_format=source_format, format=target_format,
            )

        if df.empty:
            return ConvertResult(
                success=False, error="数据为空",
                source_format=source_format, format=target_format,
            )

        content = _write_dataframe(df, target_format)

        # Size limits must never silently remove rows from an enterprise dataset.
        if config.max_size_bytes and len(content) > config.max_size_bytes:
            return ConvertResult(success=False, error="转换结果超过大小上限；原始行数必须保留，请显式创建采样任务",
                                 source_format=source_format, format=target_format,
                                 meta={"row_count": len(df), "required_bytes": len(content)})
        sampled = False

        meta = {"row_count": len(df), "column_count": len(df.columns)}
        if sampled:
            meta["sampled"] = True
            meta["note"] = f"已自动采样以符合 {config.max_size_bytes} 字节上限"

        return ConvertResult(
            success=True, content=content,
            format=target_format, source_format=source_format,
            meta=meta,
        )
    except Exception as e:
        return ConvertResult(
            success=False, error=f"数据转换错误: {e}",
            source_format=source_format, format=target_format,
        )


def _read_dataframe(data: bytes, fmt: str):
    """从字节数据读取 DataFrame。"""
    import pandas as pd

    buf = io.BytesIO(data)

    if fmt == 'csv':
        try:
            return pd.read_csv(buf, encoding='utf-8')
        except UnicodeDecodeError:
            buf.seek(0)
            return pd.read_csv(buf, encoding='gbk')
        except Exception:
            buf.seek(0)
            return pd.read_csv(buf, sep='\t', encoding='utf-8')

    elif fmt in ('xlsx', 'xls'):
        return pd.read_excel(buf, engine='openpyxl' if fmt == 'xlsx' else 'xlrd')

    elif fmt == 'json':
        try:
            return pd.read_json(buf)
        except Exception:
            buf.seek(0)
            return pd.read_json(buf, lines=True)

    elif fmt == 'xml':
        return pd.read_xml(buf)

    elif fmt == 'html':
        tables = pd.read_html(buf)
        return tables[0] if tables else None

    elif fmt == 'txt':
        try:
            return pd.read_csv(buf, sep='\t', encoding='utf-8')
        except Exception:
            buf.seek(0)
            try:
                return pd.read_csv(buf, encoding='utf-8')
            except Exception:
                text = data.decode('utf-8')
                return pd.DataFrame({'content': [line for line in text.splitlines() if line.strip()]})

    return None


def _write_dataframe(df, target_format: str) -> bytes:
    """将 DataFrame 写入字节。"""
    import pandas as pd

    buf = io.BytesIO()
    text_buf = io.StringIO()

    if target_format == 'csv':
        df.to_csv(buf, index=False, encoding='utf-8')
        return buf.getvalue()

    elif target_format == 'xlsx':
        df.to_excel(buf, index=False, engine='openpyxl')
        return buf.getvalue()

    elif target_format == 'json':
        content = df.to_json(orient='records', indent=2, force_ascii=False)
        return content.encode('utf-8')

    elif target_format == 'xml':
        content = df.to_xml(index=False, root_name='data', row_name='record')
        return content.encode('utf-8')

    elif target_format == 'html':
        html = _df_to_html(df)
        return html.encode('utf-8')

    elif target_format == 'txt':
        df.to_csv(buf, index=False, sep='\t', encoding='utf-8')
        return buf.getvalue()

    raise ValueError(f"不支持的数据目标格式: {target_format}")


def _sample_dataframe_to_fit(df, target_format: str, max_bytes: int):
    """逐步减少行数直到序列化后字节数 ≤ max_bytes。

    采样策略：均匀间隔采样子集（保留代表性），而非简单截断头部。

    Returns:
        (sampled_df, content_bytes, was_sampled: bool)
    """
    total = len(df)
    # 尝试的采样比例：100% → 50% → 25% → 12.5% → ...
    for frac in [1.0, 0.5, 0.25, 0.125, 0.06, 0.03, 0.015]:
        if frac >= 1.0:
            sampled = df
        else:
            n = max(1, int(total * frac))
            step = max(1, total // n)
            sampled = df.iloc[::step].head(n).reset_index(drop=True)

        content = _write_dataframe(sampled, target_format)
        if len(content) <= max_bytes:
            return sampled, content, (frac < 1.0)

    # 兜底：只保留第一行
    head = df.head(1)
    return head, _write_dataframe(head, target_format), True


def _fit_data_bytes(data: bytes, source_format: str, max_bytes: int) -> ConvertResult:
    """fit_size() 的数据分支：bytes → DataFrame → _sample_dataframe_to_fit → bytes。"""
    try:
        df = _read_dataframe(data, source_format)
        if df is None or df.empty:
            return ConvertResult(
                success=True, content=data, format=source_format, source_format=source_format,
                meta={"fitted": False, "note": "数据为空，无需压缩"},
            )

        content = _write_dataframe(df, source_format)
        if len(content) > max_bytes:
            return ConvertResult(success=False, error="数据超过大小上限，禁止通过隐式丢行压缩",
                                 source_format=source_format, format=source_format,
                                 meta={"row_count": len(df), "required_bytes": len(content)})
        sampled_df, was_sampled = df, False
        fitted = len(content) <= max_bytes
        return ConvertResult(
            success=True, content=content,
            format=source_format, source_format=source_format,
            meta={
                "fitted": fitted,
                "row_count": len(sampled_df),
                "column_count": len(sampled_df.columns),
                "sampled": was_sampled,
                "note": (f"已采样到 {len(sampled_df)} 行以符合 {max_bytes} 字节上限"
                         if was_sampled else "无需采样"),
            },
        )
    except Exception as e:
        return ConvertResult(success=False, error=f"数据大小拟合错误: {e}",
                           source_format=source_format, format=source_format)


def _df_to_html(df, title: str = "Data") -> str:
    """DataFrame → 带样式的 HTML。"""
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{title}</title>
    <style>
        body {{
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            max-width: 1200px;
            margin: 40px auto;
            padding: 20px;
        }}
        h1 {{ color: #333; }}
        table {{
            border-collapse: collapse;
            width: 100%;
            margin-top: 20px;
            box-shadow: 0 2px 8px rgba(0,0,0,0.1);
        }}
        th {{
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            color: white;
            padding: 12px 15px;
            text-align: left;
            font-weight: 600;
        }}
        td {{
            padding: 10px 15px;
            border-bottom: 1px solid #eee;
        }}
        tr:hover {{ background-color: #f8f9fa; }}
        tr:nth-child(even) {{ background-color: #f5f5f5; }}
    </style>
</head>
<body>
    <h1>{title}</h1>
    {df.to_html(index=False, classes='data-table', border=0)}
</body>
</html>"""


# ── C.4  PDF 转换器 ─────────────────────────────────────────────────────

def _convert_pdf(data: bytes, target_format: str, config: ConvertConfig) -> ConvertResult:
    """PDF 转换：bytes → 各目标格式 → bytes。"""
    try:
        if target_format in ('docx', 'doc'):
            return _pdf_to_docx_bytes(data)
        elif target_format == 'txt':
            return _pdf_to_text_bytes(data)
        elif target_format == 'html':
            return _pdf_to_html_bytes(data)
        elif target_format == 'md':
            return _pdf_to_md_bytes(data)
        elif target_format == 'rtf':
            return _pdf_to_rtf_bytes(data)
        elif target_format in ('png', 'jpg', 'jpeg'):
            return _pdf_to_image_bytes(data, target_format)
        else:
            return ConvertResult(success=False, error=f"PDF 不支持的目标格式: {target_format}")
    except Exception as e:
        return ConvertResult(success=False, error=f"PDF 转换错误: {e}")


def _extract_pdf_text(data: bytes) -> str:
    """从 PDF 字节中提取文本。"""
    try:
        import PyPDF2
    except ImportError:
        raise ImportError("请安装 PyPDF2: pip install PyPDF2")

    text_parts = []
    with io.BytesIO(data) as f:
        reader = PyPDF2.PdfReader(f)
        for i, page in enumerate(reader.pages):
            page_text = page.extract_text()
            if page_text:
                text_parts.append(f"--- 第 {i+1} 页 ---\n{page_text}")
    return "\n\n".join(text_parts)


def _pdf_to_docx_bytes(data: bytes) -> ConvertResult:
    """PDF → DOCX（优先 pdf2docx，备选纯文本提取）。"""
    import tempfile

    tmp_dir = tempfile.mkdtemp(prefix="pdfconv_")
    try:
        pdf_path = os.path.join(tmp_dir, "input.pdf")
        docx_path = os.path.join(tmp_dir, "output.docx")

        with open(pdf_path, 'wb') as f:
            f.write(data)

        # 方法 1: pdf2docx
        try:
            from pdf2docx import Converter
            cv = Converter(pdf_path)
            cv.convert(docx_path, start=0, end=None)
            cv.close()

            if os.path.exists(docx_path):
                with open(docx_path, 'rb') as f:
                    return ConvertResult(success=True, content=f.read(), format='docx')
        except Exception:
            pass

        # 方法 2: 纯文本回退
        try:
            text = _extract_pdf_text(data)
            if not text.strip():
                return ConvertResult(success=False, error="无法从 PDF 提取文本（可能为图像型 PDF）")

            from docx import Document
            doc = Document()
            for para in text.split('\n'):
                if para.strip():
                    doc.add_paragraph(para)
            doc.save(docx_path)

            with open(docx_path, 'rb') as f:
                return ConvertResult(
                    success=True, content=f.read(), format='docx',
                    meta={"note": "使用纯文本提取（格式可能受限）"},
                )
        except Exception as e:
            return ConvertResult(success=False, error=f"PDF→DOCX 转换失败: {e}")
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def _pdf_to_text_bytes(data: bytes) -> ConvertResult:
    """PDF → TXT。"""
    text = _extract_pdf_text(data)
    if not text.strip():
        return ConvertResult(success=False, error="无法从 PDF 提取文本")
    return ConvertResult(success=True, content=text.encode('utf-8'), format='txt')


def _pdf_to_html_bytes(data: bytes) -> ConvertResult:
    """PDF → HTML。"""
    text = _extract_pdf_text(data)
    if not text.strip():
        return ConvertResult(success=False, error="无法从 PDF 提取文本")

    paragraphs = '\n'.join(f"    <p>{p}</p>" for p in text.split('\n') if p.strip())
    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <title>PDF 转换结果</title>
    <style>
        body {{ font-family: sans-serif; max-width: 800px; margin: 40px auto; padding: 20px; line-height: 1.6; }}
        p {{ margin: 1em 0; }}
    </style>
</head>
<body>
{paragraphs}
</body>
</html>"""
    return ConvertResult(success=True, content=html.encode('utf-8'), format='html')


def _pdf_to_md_bytes(data: bytes) -> ConvertResult:
    """PDF → Markdown。"""
    text = _extract_pdf_text(data)
    if not text.strip():
        return ConvertResult(success=False, error="无法从 PDF 提取文本")

    md = "# PDF 转换结果\n\n"
    for para in text.split('\n'):
        if para.strip():
            md += f"{para}\n\n"

    return ConvertResult(success=True, content=md.encode('utf-8'), format='md')


def _pdf_to_rtf_bytes(data: bytes) -> ConvertResult:
    """PDF → RTF。"""
    text = _extract_pdf_text(data)
    if not text.strip():
        return ConvertResult(success=False, error="无法从 PDF 提取文本")

    rtf = "{\\rtf1\\ansi\\deff0\n"
    for para in text.split('\n'):
        if para.strip():
            rtf += f"\\par {para}\n"
    rtf += "}"

    return ConvertResult(success=True, content=rtf.encode('utf-8'), format='rtf')


def _pdf_to_image_bytes(data: bytes, target_format: str) -> ConvertResult:
    """PDF → 图片（使用 PyMuPDF）。"""
    try:
        import fitz  # PyMuPDF
    except ImportError:
        return ConvertResult(success=False, error="PDF→图片需要 PyMuPDF: pip install PyMuPDF")

    try:
        pdf_doc = fitz.open(stream=data, filetype="pdf")
        if len(pdf_doc) == 0:
            return ConvertResult(success=False, error="PDF 文档为空")

        # 默认只转换第一页；多页可用 config.pdf_page_range
        mat = fitz.Matrix(2.0, 2.0)
        pix = pdf_doc[0].get_pixmap(matrix=mat)

        img_bytes = pix.tobytes(output=target_format if target_format != 'jpg' else 'jpeg')

        meta = {}
        if len(pdf_doc) > 1:
            meta["note"] = f"仅转换了第 1 页（共 {len(pdf_doc)} 页）"

        pdf_doc.close()
        return ConvertResult(success=True, content=img_bytes, format=target_format, meta=meta)

    except Exception as e:
        return ConvertResult(success=False, error=f"PDF→图片错误: {e}")


# ── C.5  DOCX 转换器 ─────────────────────────────────────────────────────

def _convert_docx(data: bytes, target_format: str, config: ConvertConfig | None = None) -> ConvertResult:
    """DOCX 转换：bytes → 各目标格式 → bytes。"""
    import tempfile

    tmp_dir = tempfile.mkdtemp(prefix="docxconv_")
    try:
        docx_path = os.path.join(tmp_dir, "input.docx")
        output_path = os.path.join(tmp_dir, f"output.{target_format}")

        with open(docx_path, 'wb') as f:
            f.write(data)

        if target_format == 'pdf':
            return _docx_to_pdf(docx_path, output_path, data)
        elif target_format == 'txt':
            return _docx_to_txt_from_bytes(data)
        elif target_format == 'html':
            return _docx_to_html_from_bytes(data)
        elif target_format == 'md':
            return _docx_to_md_from_bytes(data)
        else:
            return ConvertResult(success=False, error=f"DOCX 不支持的目标格式: {target_format}")
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def _docx_to_pdf(docx_path: str, output_path: str, original_data: bytes) -> ConvertResult:
    """DOCX → PDF：优先 docx2pdf (Word)，备选 LibreOffice。"""
    # 方法 1: docx2pdf (Windows + Word)
    try:
        from docx2pdf import convert as docx2pdf_convert
        docx2pdf_convert(docx_path, output_path)
        if os.path.exists(output_path):
            with open(output_path, 'rb') as f:
                return ConvertResult(success=True, content=f.read(), format='pdf')
    except Exception:
        pass

    # 方法 2: LibreOffice 异步子进程
    soffice = _find_libreoffice()
    if soffice:
        try:
            out_dir = os.path.dirname(output_path)
            result = subprocess.run(
                [soffice, "--headless", "--convert-to", "pdf", "--outdir", out_dir, docx_path],
                capture_output=True, timeout=120,
                check=True,
            )
            expected = os.path.join(out_dir, os.path.splitext(os.path.basename(docx_path))[0] + ".pdf")
            if os.path.exists(expected):
                if expected != output_path:
                    shutil.move(expected, output_path)
                with open(output_path, 'rb') as f:
                    return ConvertResult(success=True, content=f.read(), format='pdf')
        except Exception:
            pass

    return ConvertResult(success=False, error="PDF 转换需要 Microsoft Word 或 LibreOffice")


def _find_libreoffice() -> str | None:
    """查找 LibreOffice 安装路径。"""
    candidates = [
        r"C:\Program Files\LibreOffice\program\soffice.exe",
        r"C:\Program Files (x86)\LibreOffice\program\soffice.exe",
        "soffice",
        "/usr/bin/soffice",
        "/Applications/LibreOffice.app/Contents/MacOS/soffice",
    ]
    for path in candidates:
        if os.path.exists(path) or shutil.which(path):
            return path
    return None


def _docx_to_txt_from_bytes(data: bytes) -> ConvertResult:
    """DOCX → TXT（内存操作）。"""
    try:
        from docx import Document
    except ImportError:
        return ConvertResult(success=False, error="请安装 python-docx: pip install python-docx")

    doc = Document(io.BytesIO(data))
    lines = []

    for para in doc.paragraphs:
        lines.append(para.text)

    for table in doc.tables:
        for row in table.rows:
            row_text = '\t'.join(cell.text for cell in row.cells)
            lines.append(row_text)

    content = '\n'.join(lines).encode('utf-8')
    return ConvertResult(success=True, content=content, format='txt')


def _docx_to_html_from_bytes(data: bytes) -> ConvertResult:
    """DOCX → HTML（内存操作）。"""
    try:
        from docx import Document
    except ImportError:
        return ConvertResult(success=False, error="请安装 python-docx: pip install python-docx")

    doc = Document(io.BytesIO(data))

    parts = ["""<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>DOCX 转换结果</title>
    <style>
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            max-width: 800px; margin: 40px auto; padding: 20px; line-height: 1.6;
        }
        h1 { font-size: 2em; margin-top: 1em; }
        h2 { font-size: 1.5em; margin-top: 0.8em; }
        h3 { font-size: 1.2em; margin-top: 0.6em; }
        p { margin: 1em 0; }
        table { border-collapse: collapse; width: 100%%; margin: 1em 0; }
        th, td { border: 1px solid #ddd; padding: 8px; text-align: left; }
        th { background-color: #f5f5f5; }
    </style>
</head>
<body>
"""]

    for para in doc.paragraphs:
        if para.text.strip():
            style = para.style.name if para.style else ""
            if "Heading 1" in style:
                parts.append(f"    <h1>{para.text}</h1>")
            elif "Heading 2" in style:
                parts.append(f"    <h2>{para.text}</h2>")
            elif "Heading 3" in style:
                parts.append(f"    <h3>{para.text}</h3>")
            else:
                parts.append(f"    <p>{para.text}</p>")

    for table in doc.tables:
        parts.append("    <table>")
        for i, row in enumerate(table.rows):
            parts.append("        <tr>")
            tag = "th" if i == 0 else "td"
            for cell in row.cells:
                parts.append(f"            <{tag}>{cell.text}</{tag}>")
            parts.append("        </tr>")
        parts.append("    </table>")

    parts.append("</body>\n</html>")

    content = '\n'.join(parts).encode('utf-8')
    return ConvertResult(success=True, content=content, format='html')


def _docx_to_md_from_bytes(data: bytes) -> ConvertResult:
    """DOCX → Markdown（内存操作）。"""
    try:
        from docx import Document
    except ImportError:
        return ConvertResult(success=False, error="请安装 python-docx: pip install python-docx")

    doc = Document(io.BytesIO(data))
    md_parts = ["# DOCX 转换结果\n"]

    for para in doc.paragraphs:
        if para.text.strip():
            style = para.style.name if para.style else ""
            if "Heading 1" in style:
                md_parts.append(f"# {para.text}\n")
            elif "Heading 2" in style:
                md_parts.append(f"## {para.text}\n")
            elif "Heading 3" in style:
                md_parts.append(f"### {para.text}\n")
            else:
                md_parts.append(f"{para.text}\n")

    for table in doc.tables:
        if table.rows:
            header = [cell.text for cell in table.rows[0].cells]
            md_parts.append("| " + " | ".join(header) + " |")
            md_parts.append("| " + " | ".join(["---"] * len(header)) + " |")
            for row in table.rows[1:]:
                cells = [cell.text for cell in row.cells]
                md_parts.append("| " + " | ".join(cells) + " |")
        md_parts.append("")

    content = '\n'.join(md_parts).encode('utf-8')
    return ConvertResult(success=True, content=content, format='md')


# ── C.6  便捷函数：DataFrame 直接转换 ───────────────────────────────────

def convert_dataframe(
    df,
    target_format: str,
) -> ConvertResult:
    """直接从 pandas DataFrame 转换，跳过字节解析步骤。

    这是数据平台最常见的使用场景：从数据库/SQL 查询获得 DataFrame 后直接转换。

    Args:
        df: pandas DataFrame
        target_format: 目标格式（csv, xlsx, json, xml, html, txt）

    Returns:
        ConvertResult

    Example:
        >>> import pandas as pd
        >>> df = pd.DataFrame({"id": [1, 2], "name": ["Alice", "Bob"]})
        >>> result = convert_dataframe(df, "json")
        >>> result.content
        b'[\\n  {\\n    "id": 1,\\n    "name": "Alice"\\n  },...\\n]'
    """
    try:
        content = _write_dataframe(df, target_format)
        return ConvertResult(
            success=True, content=content,
            format=target_format, source_format="dataframe",
            meta={"row_count": len(df), "column_count": len(df.columns)},
        )
    except Exception as e:
        return ConvertResult(success=False, error=f"DataFrame 转换错误: {e}",
                             format=target_format)

# ── 上游数据源协议 ─────────────────────────────────────────────────────────

class UpstreamDataSource(Protocol):
    """读取上游节点数据的协议 —— 你可以传入自定义实现，也可以直接用默认的 DB/MinIO 版本。

    返回 list[FileItem] 支持混合格式数据集场景。
    如果只需要单文件，返回只含一个元素的列表。
    """

    async def fetch(self, pre_node: str, flow_id: str, exec_version: int) -> list[FileItem]:
        """根据上游节点名和版本号，返回文件列表。"""
        ...


class MinioSourceProtocol(Protocol):
    """MinIO 上游数据源协议。"""

    async def fetch_bytes(self, bucket: str, object_key: str) -> bytes:
        """从 MinIO 下载单个对象，返回原始字节。"""
        ...


# ── 节点类 ──────────────────────────────────────────────────────────────────

def _serialize_results(results):
    """同步序列化转换结果为 JSON 字符串列表（在线程池中执行）。"""
    import json as _json
    import base64 as _b64
    rows = []
    for fi, r in results:
        if not r.success:
            continue
        fmt = r.format or ""
        sf = r.source_format or fi.source_format
        file_type = detect_type(f".{fmt}") if fmt else FileType.UNKNOWN
        if file_type == FileType.DATA:
            try:
                text = r.content.decode("utf-8", errors="replace")
                parsed = _json.loads(text)
                if isinstance(parsed, list):
                    if len(parsed) == 0:
                        rows.append(_json.dumps({"_empty": True, "_format": fmt, "_source_format": sf, "_name": fi.name}, ensure_ascii=False))
                    else:
                        for rec in parsed:
                            if isinstance(rec, dict):
                                rows.append(_json.dumps(rec, ensure_ascii=False))
                elif isinstance(parsed, dict):
                    rows.append(_json.dumps(parsed, ensure_ascii=False))
                else:
                    rows.append(_json.dumps({"content": text}, ensure_ascii=False))
            except (_json.JSONDecodeError, UnicodeDecodeError):
                rows.append(_json.dumps({"content_b64": _b64.b64encode(r.content).decode("utf-8"), "_format": fmt, "_source_format": sf}, ensure_ascii=False))
        else:
            rows.append(_json.dumps({"content_b64": _b64.b64encode(r.content).decode("utf-8"), "_format": fmt, "_source_format": sf, "_name": fi.name, "_size": len(r.content)}, ensure_ascii=False))
    return rows


class ConverterNode:
    """将 converter_core 包装为数据管道节点。

    参照 DataStorage 的 self.pre_node + load_previous_node_data() + run() 模式。
    同时支持 MySQL（结构化数据）和 MinIO（非结构化数据）作为上游数据来源。

    关键改进（v2）：
    - 去掉 DataFactoryProgressTracker 硬依赖，上游元信息通过 context 传入
    - 支持混合格式数据集：按 source_format 分组，异步并发转换
    - fit_size() 独立接口，与 convert() 解耦

    属性一览：
    ┌──────────────────┬──────────────────────────────────────────────┐
    │ self.xxx         │ 角色                                         │
    ├──────────────────┼──────────────────────────────────────────────┤
    │ self.pre_node    │ 连接上游 —— 定位上一节点的输出数据             │
    │ self.flow_id     │ 流程标识 —— 贯穿日志、表名、进度               │
    │ self.config      │ 转换参数 —— source_format / target_format    │
    │ self.context     │ 上下文   —— exec_version 等                  │
    │ self.target_type │ 写回目标 —— ["mysql"] / ["minio"] / 双写     │
    │ self.result_table│ 自身输出表名 —— 供下游节点发现                 │
    │ self.conv_config │ ConvertConfig 实例 —— 转换质量/大小控制       │
    └──────────────────┴──────────────────────────────────────────────┘
    """

    # 节点类型名（用于注册到 DATA_FACTORY_NODE_MAPPING）
    NODE_TYPE: str = "converter"

    def __init__(
        self,
        pre_node: str,
        flow_id: str,
        config: dict[str, Any],
        context: dict[str, Any],
        **kwargs,
    ):
        """初始化转换节点。

        Args:
            pre_node: 上游节点名称，用于定位上游版本表
            flow_id:   流程唯一标识（字母数字下划线）
            config:    用户在 UI 上填的转换配置（可全部留空，自动推断）
                ┌────────────────────┬──────┬──────────────────────────┐
                │ 字段                │ 必填  │ 说明                      │
                ├────────────────────┼──────┼──────────────────────────┤
                │ source_format      │ 否   │ 上游数据格式，不填自动推断                  │
                │ source_format_filter│否   │ 只转换指定格式，如["csv","json"]，不填=全部    │
                │ target_format      │ 是   │ 输出格式(csv/json/xlsx/...)，用户在下拉框选择  │
                │ converter_config   │ 否   │ 转换参数子字典 (见 ConvertConfig)        │
                │   .quality         │ 否   │ 图像/视频质量 1-100, 默认95              │
                │   .max_size_bytes  │ 否   │ 文件大小上限(字节)，超过自动压缩          │
                │   .target_dimensions│ 否  │ 图像统一尺寸(宽,高), 如(24,24)           │
                │ minio_rule         │ 否   │ MinIO 规则: {prefix, field}              │
                │ concurrency        │ 否   │ 并发数, 默认5                             │
                │ max_retries        │ 否   │ 失败重试次数, 默认3                       │
                └────────────────────┴──────┴──────────────────────────┘
            context: 框架上下文
                ┌──────────────────┬──────┬──────────────────────────┐
                │ 字段              │ 必填  │ 说明                    │
                ├──────────────────┼──────┼──────────────────────────┤
                │ exec_version     │ 否   │ 执行版本号, 默认1         │
                │ upstream_meta    │ 是   │ 上游表元信息 ↓            │
                │   .source_table  │ 是   │ 上游版本表名，如 \"flow_xxx_collector_v1\" │
                └──────────────────┴──────┴──────────────────────────┘
            **kwargs: 框架注入的外部依赖
                ┌─────────────────────┬──────┬──────────────────────────┐
                │ 参数                 │ 必填  │ 说明                    │
                ├─────────────────────┼──────┼──────────────────────────┤
                │ flow_config         │ 是   │ 完整流程 DAG 配置(dict)，用于自动推断参数  │
                │ db                  │ 是*  │ SQLAlchemy 异步 session   │
                │ db_config           │ 是*  │ 数据库连接配置 dict        │
                │ minio_client        │ 是*  │ AsyncMinIOClient 实例     │
                │ minio_config        │ 是*  │ {minio_host, minio_port,  │
                │                     │      │  minio_user, minio_password,│
                │                     │      │  minio_bucket}            │
                │ node_table_mapping  │ 否   │ 节点名→表名前缀映射 dict    │
                │ logger              │ 否   │ async func(level, msg) 日志│
                │ upstream            │ 否   │ UpstreamDataSource 实例    │
                │ minio_source        │ 否   │ MinioSourceProtocol 实例   │
                └─────────────────────┴──────┴──────────────────────────┘
                * db + db_config 或 minio_client + minio_config 至少提供一组
        """
        # ── 管道连接 ──
        self.pre_node = pre_node                                  # 上游节点名称
        self.flow_id = flow_id                                    # 流程 ID
        self.config = config                                      # 配置字典
        self.context = context                                    # 上下文

        # ── 转换参数 ──
        # target_type 永远自动推断（下游节点决定，用户改不了）
        # source_format / target_format 用户可选覆盖，否则自动推断
        self._source_format = config.get("source_format")         # None = 自动推断
        self._target_format = config.get("target_format")         # None = 自动推断
        self._target_format_map = config.get("target_format_map")  # None=全局target_format, dict={csv:"json",jpg:"webp"}
        self._source_format_filter = config.get("source_format_filter")  # None=全部, list[str]=只转指定格式
        self.converter_config = config.get("converter_config")    # 可选的 ConvertConfig 参数

        # ── MinIO 配置（需要时通过 kwargs 注入） ──
        self.minio_rule = config.get("minio_rule", {})            # {prefix, field}
        self._minio_client = kwargs.get("minio_client")           # AsyncMinIOClient（注入）
        self._minio_config = kwargs.get("minio_config", {})       # 完整的 MinIO 连接配置

        # ── 并发控制 ──
        self._concurrency = config.get("concurrency", 5)          # 异步转换并发数
        self._max_retries = config.get("max_retries", 3)          # 失败自动重试次数
        self._semaphore: asyncio.Semaphore | None = None          # 延迟初始化
        self._executor: ThreadPoolExecutor | None = None          # 延迟初始化（与并发数匹配）

        # ── 版本管理（与 DataStorage 规则一致） ──
        self.exec_version = self.context.get("exec_version", 1)
        safe_flow_id = _validate_flow_id(flow_id)
        self.result_table = f"flow_{safe_flow_id}_converter_v{self.exec_version}"
        _validate_identifier(self.result_table, max_len=128)

        # 上一版本（用于恢复失败数据）
        self.last_version = self.exec_version - 1
        self.last_version_table = (
            f"flow_{safe_flow_id}_converter_v{self.last_version}"
            if self.last_version >= 1 else None
        )

        # ── 上游元信息（框架通过 context 传入，不再强依赖 DataFactoryProgressTracker） ──
        # context["upstream_meta"] = {
        #     "source_table": "flow_xxx_data_collector_v1",
        #     "exec_version": 1,
        # }
        self._upstream_meta = self.context.get("upstream_meta", {})

        # ── 外部依赖（通过 kwargs 注入） ──
        self.db = kwargs.get("db")                                # 数据库会话
        self.db_config = kwargs.get("db_config")                  # 数据库配置
        self._upstream = kwargs.get("upstream")                   # 自定义上游数据源（可选）
        self._minio_source = kwargs.get("minio_source")           # 自定义 MinIO 上游（可选）
        self._node_table_mapping = kwargs.get("node_table_mapping", {})  # 节点名 → 表名前缀映射
        self._flow_config = kwargs.get("flow_config", {})         # 完整流程配置（用于自动推断参数）
        self._logger = kwargs.get("logger")                       # 自定义日志函数（可选）

        # 自动推断结果缓存
        self._resolved = False
        self._source_format_val: str = ""
        self._target_type_val: list[str] = []
        self._target_format_val: str = ""


    @property
    def source_format(self) -> str:
        if not self._resolved:
            raise RuntimeError("转换参数尚未解析，请先调用 run() → _resolve_params()")
        return self._source_format_val

    @property
    def target_type(self) -> list[str]:
        if not self._resolved:
            raise RuntimeError("转换参数尚未解析，请先调用 run() → _resolve_params()")
        return self._target_type_val

    @property
    def target_format(self) -> str:
        if not self._resolved:
            raise RuntimeError("转换参数尚未解析，请先调用 run() → _resolve_params()")
        return self._target_format_val

    def _get_executor(self) -> ThreadPoolExecutor:
        """延迟初始化线程池，大小 = 并发数，避免 run_in_executor 用默认池。"""
        if self._executor is None:
            self._executor = ThreadPoolExecutor(max_workers=self._concurrency)
        return self._executor

    async def _run_in_thread(self, fn, *args):
        """在专用线程池中执行同步函数（convert/fit_size 是 CPU-bound）。"""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._get_executor(), fn, *args)

    # ── 便捷属性 ───────────────────────────────────────────────────────────

    @property
    def conv_config(self) -> ConvertConfig:
        """延迟构造 ConvertConfig —— 只在真正需要转换时才创建。"""
        if not hasattr(self, "_conv_config"):
            cc = self.converter_config or {}
            self._conv_config = ConvertConfig(
                timeout_seconds=cc.get("timeout_seconds", 300),
                quality=cc.get("quality", 95),
                max_size_bytes=cc.get("max_size_bytes"),
                target_dimensions=cc.get("target_dimensions"),
                csv_delimiter=cc.get("csv_delimiter"),
            )
        return self._conv_config

    # ── 参数自动推断 ───────────────────────────────────────────────────────

    async def _resolve_params(self) -> None:
        """根据上下游节点信息自动推断转换参数。

        推断规则（优先级从高到低）：
          1. 用户在 config 中显式指定 → 直接使用
          2. 从 _flow_config 读上下游节点的配置
          3. 从上游版本表采样数据自动识别
        """
        if self._resolved:
            return

        upstream_config = self._flow_config.get(self.pre_node, {})


        # ═══ source_format ═══  — 用户可选覆盖
        if self._source_format:
            self._source_format_val = self._source_format
        else:
            sf = upstream_config.get("source_format", "")
            if not sf and self.db is not None:
                sf = await self._detect_source_format_from_data()
            self._source_format_val = sf.lower().lstrip(".") if sf else ""

        # ═══ target_format ═══  — 用户可选覆盖
        if self._target_format:
            self._target_format_val = self._target_format
        else:
            downstream_format = ""
            for node_name, node_cfg in self._flow_config.items():
                if node_cfg.get("pre_node") == "converter" and node_name != self.pre_node:
                    downstream_format = node_cfg.get("source_format", "")
                    break
            if not downstream_format and self._source_format_val:
                ft = detect_type(f".{self._source_format_val}")
                formats = get_formats(ft)
                downstream_format = formats[0] if formats else "json"
            self._target_format_val = downstream_format.lower().lstrip(".")

        # ═══ target_type ═══  — 永远自动推断：下游从哪读就往哪写
        downstream_type = None
        for node_name, node_cfg in self._flow_config.items():
            if node_cfg.get("pre_node") == "converter" and node_name != self.pre_node:
                downstream_type = node_cfg.get("source_type", "mysql")
                break
        if downstream_type:
            self._target_type_val = [downstream_type] if isinstance(downstream_type, str) else downstream_type
        else:
            self._target_type_val = ["mysql"]

        self._resolved = True

        await self._log(
            "info",
            f"参数推断完成: {self.source_format} → {self.target_format}:{self.target_type}",
        )

    async def _detect_source_format_from_data(self) -> str:
        """从上游版本表采样第一条数据，读取 format 字段。

        context["upstream_meta"]["source_table"] 由框架层传入，
        直接包含上游表名，无需查询进度表。
        """
        if self.db is None:
            return ""

        source_table = self._upstream_meta.get("source_table", "")
        if not source_table:
            return ""
        source_table = _validate_identifier(source_table, max_len=128)

        try:
            from sqlalchemy import text
            sql = text(f"SELECT data FROM `{source_table}` LIMIT 1")
            result = await self.db.execute(sql)
            row = result.fetchone()
            if row:
                data_val = row[0]
                if isinstance(data_val, str):
                    try:
                        data_val = json.loads(data_val)
                    except (json.JSONDecodeError, TypeError):
                        pass
                if isinstance(data_val, dict):
                    fmt = data_val.get("format", "")
                    if fmt:
                        return fmt
                    path = data_val.get("path", "")
                    if path and "." in path:
                        ext = path.rsplit(".", 1)[-1].split("?")[0]
                        if len(ext) <= 6:
                            return ext
        except Exception:
            pass
        return ""

    # ── 核心入口 ───────────────────────────────────────────────────────────

    async def run(self) -> dict[str, Any]:
        """节点统一入口：读上游 → 流式转换 → 写回 → 返回摘要。

        两阶段处理：
        1. 内联数据（MySQL content_b64）→ 全载内存，分组批量转换
        2. MinIO 引用 → 流式 pipeline：逐个下载→转换→保存→释放
           （最多 _concurrency 个文件同时在内存）
        """
        try:
            await self._resolve_params()
            await self._log("info", f"开始执行格式转换节点（target={self.target_type}）")

            if "mysql" in self.target_type:
                await self._create_result_table()

            # 读上游 → 分离内联数据和 MinIO 引用
            inline_files, minio_refs = await self.load_previous_node_data()

            if not inline_files and not minio_refs:
                raise ValueError("上游节点无数据，无法执行转换")

            # ═══ 断点续跑：跳过上一版本已成功的文件 ═══
            failed_from_history = await self._load_history_failed_files()
            if failed_from_history:
                await self._log(
                    "info",
                    f"断点续跑模式：跳过已成功的，只处理 {len(failed_from_history)} 个失败文件",
                )
                # 过滤内联数据
                inline_files = [f for f in inline_files if f.name in failed_from_history]
                # 过滤 MinIO 引用
                minio_refs = [r for r in minio_refs if r["name"] in failed_from_history]

            # ═══ 格式过滤：用户只转换选中的格式 ═══
            if self._source_format_filter:
                selected = set(self._source_format_filter)
                inline_files = [f for f in inline_files if f.source_format in selected]
                minio_refs = [r for r in minio_refs if r["format"] in selected]
                await self._log("info",
                    f"格式过滤: {list(selected)}, 剩余 {len(inline_files)} 个内联 + {len(minio_refs)} 个 MinIO")

            if not inline_files and not minio_refs:
                await self._log("info", "所有文件已在上一版本成功处理，跳过")
                return {
                    "status": "success",
                    "node_type": self.NODE_TYPE,
                    "flow_id": self.flow_id,
                    "total_files": 0,
                    "success_count": 0,
                    "failed_count": 0,
                    "result_table": self.result_table,
                    "files": [],
                }

            all_files: list[dict] = []  # 汇总用
            total_input = 0
            total_output = 0

            # ═══ 阶段 1：内联数据（已在内存，分组批量转换） ═══
            if inline_files:
                total_input += sum(len(f.data) for f in inline_files)
                await self._log("info", f"内联数据 {len(inline_files)} 个文件，分组批量转换")
                inline_results = await self._group_and_convert(inline_files)
                await self._save_results(inline_results)
                for fi, r in inline_results:
                    all_files.append({
                        "name": fi.name, "format": r.format, "success": r.success,
                        "size": len(r.content) if r.content else 0,
                        "error": r.error, "meta": r.meta or {},
                    })
                    if r.content:
                        total_output += len(r.content)

            # ═══ 阶段 2：MinIO 文件（流式：下载→转换→保存→释放） ═══
            if minio_refs:
                await self._log("info", f"MinIO 文件 {len(minio_refs)} 个，流式处理（并发={self._concurrency}）")
                minio_results = await self._stream_minio_pipeline(minio_refs)
                for ref, r in minio_results:
                    all_files.append({
                        "name": ref["name"], "format": r.format, "success": r.success,
                        "size": len(r.content) if r.content else 0,
                        "error": r.error, "meta": r.meta or {},
                    })
                    if r.content:
                        total_input += len(r.content)
                        total_output += len(r.content)
                    elif r.meta and r.meta.get("size"):
                        total_output += r.meta["size"]

            # 汇总
            success_count = sum(1 for f in all_files if f["success"])
            failed_count = len(all_files) - success_count

            return {
                "status": "success" if failed_count == 0 else "partial",
                "node_type": self.NODE_TYPE,
                "flow_id": self.flow_id,
                "pre_node": self.pre_node,
                "source_format": self.source_format,
                "target_format": self.target_format,
                "target_type": self.target_type,
                "total_files": len(all_files),
                "success_count": success_count,
                "failed_count": failed_count,
                "input_size": total_input,
                "output_size": total_output,
                "exec_version": self.exec_version,
                "result_table": self.result_table,
                "files": all_files,
            }

        except Exception as e:
            await self._log("error", f"转换节点执行失败: {e}")
            raise

    # ════════════════════════════════════════════════════════════════════════
    # MinIO 流式处理 pipeline
    # ════════════════════════════════════════════════════════════════════════

    async def _stream_minio_pipeline(
        self, refs: list[dict]
    ) -> list[tuple[dict, ConvertResult]]:
        """逐文件流式 pipeline：下载 → 转换 → 保存 → 释放 → 下一个。

        每个文件独立走完全流程后才释放 Semaphore 槽位，
        保证同时最多 _concurrency 个文件在内存中。

        每文件内部自带重试（下载一次，convert 重试 N 次），
        成功立即写回，失败记录到返回值。
        """
        if not refs:
            return []

        if self._minio_client is None:
            raise RuntimeError(
                "上游表中有 MinIO 路径引用，但未提供 minio_client。"
                "请通过 kwargs 注入 AsyncMinIOClient 实例。"
            )
        if self._semaphore is None:
            self._semaphore = asyncio.Semaphore(self._concurrency)

        max_retries = getattr(self, '_max_retries', 3)
        results: list[tuple[dict, ConvertResult]] = []

        async def process_one(ref: dict) -> None:
            """单个文件的完整 pipeline：下载→[重试]转换→保存→释放。"""
            name = ref["name"]
            fmt = ref["format"]
            # ① 预检：格式是否支持（按 source_format 查 target_format_map）
            tf = self._get_target_format(fmt)
            unsupported = _check_format_support(fmt, tf)
            if unsupported:
                await self._log("error", f"[{name}] ✗ 格式不支持: {unsupported}")
                results.append((ref, ConvertResult(
                    success=False, error=unsupported,
                    source_format=fmt, format=tf,
                )))
                return

            async with self._semaphore:
                # ② 下载
                await self._log("info", f"[{name}] ② 下载中 ({fmt}, {ref['bucket']}/{ref['object_key']})...")
                try:
                    data = await self._minio_client.download_data(
                        ref["bucket"], ref["object_key"]
                    )
                except Exception as e:
                    await self._log("error", f"[{name}] ✗ 下载失败: {e}")
                    results.append((ref, ConvertResult(
                        success=False, error=f"MinIO 下载异常: {e}",
                        source_format=fmt, format=tf,
                    )))
                    return

                if not data:
                    await self._log("error", f"[{name}] ✗ 下载返回空数据")
                    results.append((ref, ConvertResult(
                        success=False, error="MinIO 下载返回空数据",
                        source_format=fmt, format=tf,
                    )))
                    return
                await self._log("info", f"[{name}] ② 下载完成 ({len(data)} bytes)")

                # ③ 转换 + 重试
                action = "fit_size" if fmt == tf else "convert"
                await self._log("info", f"[{name}] ③ {action}({fmt} → {tf})...")
                last_error = None
                for attempt in range(max_retries + 1):
                    if attempt > 0:
                        await self._log("info",
                            f"[{name}] ③ 重试第 {attempt + 1} 轮...")
                    try:
                        result = await self._run_in_thread(
                            self._apply_conversion, data, fmt)
                    except Exception as e:
                        last_error = str(e)
                        result = ConvertResult(
                            success=False, error=str(e),
                            source_format=fmt, format=tf,
                        )

                    if result.success:
                        out_size = len(result.content) if result.content else 0
                        await self._log("info",
                            f"[{name}] ③ 转换完成 ({len(data)} → {out_size} bytes)")
                        # ③ 保存
                        await self._log("info", f"[{name}] ④ 保存中...")
                        fi = FileItem(name=name, data=data, source_format=fmt)
                        await self._save_results([(fi, result)])
                        await self._log("info", f"[{name}] ✓ 完成")
                        # 只保留元数据，不保留 content bytes（已在 save 中写入）
                        results.append((ref, ConvertResult(
                            success=True,
                            content=None,
                            format=result.format,
                            source_format=result.source_format,
                            meta={"size": out_size, **result.meta} if result.meta else {"size": out_size},
                        )))
                        return

                    last_error = result.error
                    await self._log("warning", f"[{name}] ② 失败: {last_error}")
                    if attempt < max_retries:
                        await asyncio.sleep(2 ** attempt)

                # 重试耗尽
                await self._log("error",
                    f"[{name}] ✗ 重试 {max_retries} 次后仍失败: {last_error}")
                results.append((ref, ConvertResult(
                    success=False,
                    error=f"重试 {max_retries} 次后仍失败: {last_error}",
                    source_format=fmt, format=tf,
                )))
                # data 离开作用域 → GC 回收

        # 所有文件并发 pipeline（Semaphore 限制实际并发数）
        await asyncio.gather(*[process_one(r) for r in refs])

        return results

    # ════════════════════════════════════════════════════════════════════════
    # 文件分组 + 异步转换
    # ════════════════════════════════════════════════════════════════════════

    async def _group_and_convert(
        self, files: list[FileItem]
    ) -> list[tuple[FileItem, ConvertResult]]:
        """按 source_format 分组 → 转换 → 失败重试 → 返回最终结果。

        分组策略：
        - 同格式 DATA 多行：DataFrame 批量转换（失败回退逐文件）
        - Binary / 单文件：逐个独立 convert()

        重试策略：
        - 第一轮按组并发转换
        - 失败的文件逐文件重试（最多 self._max_retries 次）
        - 每次重试间隔递增（1s, 2s, 4s...）
        """
        if self._semaphore is None:
            self._semaphore = asyncio.Semaphore(self._concurrency)

        # Step 1: 按 source_format 分组
        groups: dict[str, list[FileItem]] = {}
        for f in files:
            sf = f.source_format or self.source_format
            groups.setdefault(sf, []).append(f)

        await self._log("info", f"文件分组完成：{[(k, len(v)) for k, v in groups.items()]}")

        # Step 2: 每个组异步转换
        async def convert_group(sf: str, group_files: list[FileItem]):
            ft = detect_type(f".{sf}")
            names = [f.name for f in group_files]
            await self._log("info",
                f"  [{sf}] 开始处理 {len(group_files)} 个文件: {names}")
            if ft == FileType.DATA and len(group_files) > 1:
                await self._log("info", f"  [{sf}] DATA 批量合并 DataFrame...")
                result = await self._convert_data_group(group_files, sf)
            else:
                result = await self._convert_binary_group(group_files, sf)
            ok = sum(1 for _, r in result if r.success)
            await self._log("info", f"  [{sf}] 完成: {ok}/{len(result)} 成功")
            return result

        tasks = [convert_group(sf, g) for sf, g in groups.items()]
        group_results = await asyncio.gather(*tasks)

        # Step 3: 拆分成功/失败
        all_results: list[tuple[FileItem, ConvertResult]] = []
        failed: list[FileItem] = []
        for gr in group_results:
            for fi, r in gr:
                if r.success:
                    all_results.append((fi, r))
                else:
                    failed.append(fi)

        # Step 4: 失败重试（逐文件，带退避）
        if failed:
            max_retries = getattr(self, '_max_retries', 3)
            await self._log("info",
                f"第一轮 {len(failed)} 个失败，开始重试（最多 {max_retries} 次）: "
                f"{[f.name for f in failed]}")

            for attempt in range(max_retries):
                if not failed:
                    break
                await asyncio.sleep(2 ** attempt)
                await self._log("info",
                    f"重试第 {attempt + 2} 轮：{len(failed)} 个文件")

                retry_results = await self._retry_failed(failed)
                still_failed = []
                for fi, r in retry_results:
                    if r.success:
                        all_results.append((fi, r))
                    else:
                        still_failed.append(fi)
                failed = still_failed

            if failed:
                await self._log("warning",
                    f"{len(failed)} 个文件重试 {max_retries} 次后仍失败："
                    f"{[f.name for f in failed]}")
                for fi in failed:
                    all_results.append((fi, ConvertResult(
                        success=False,
                        error=f"重试 {max_retries} 次后仍失败",
                        source_format=fi.source_format,
                        format=self.target_format,
                    )))

        return all_results

    async def _retry_failed(
        self, files: list[FileItem]
    ) -> list[tuple[FileItem, ConvertResult]]:
        """对失败的文件逐文件重试转换（不分组，逐个独立跑）。"""
        sem = self._semaphore

        async def retry_one(f: FileItem) -> tuple[FileItem, ConvertResult]:
            async with sem:
                try:
                    result = await self._run_in_thread(
                        self._apply_conversion, f.data,
                        f.source_format or self.source_format)
                    return f, result
                except Exception as e:
                    return f, ConvertResult(
                        success=False, error=f"重试异常: {e}",
                        source_format=f.source_format or self.source_format,
                        format=self.target_format,
                    )

        tasks = [retry_one(f) for f in files]
        return list(await asyncio.gather(*tasks))

    # ── 转换路由：同格式 fit_size / 不同格式 convert ──────────────────────

    def _get_target_format(self, source_format: str) -> str:
        """根据 source_format 获取对应的 target_format。

        优先查 target_format_map（用户按格式指定），没有则用全局 target_format。
        """
        sf = source_format.lower().lstrip('.')
        if self._target_format_map and sf in self._target_format_map:
            return self._target_format_map[sf].lower().lstrip('.')
        return self.target_format

    def _apply_conversion(self, data: bytes, source_format: str) -> ConvertResult:
        """根据 source/target 格式自动路由到正确的处理函数。

        - source_format != target_format → convert() 格式转换
        - source_format == target_format + 有 fit 参数 → fit_size() 只调大小
        - source_format == target_format + 无参数 → 直接返回（无需操作）
        """
        sf = source_format.lower().lstrip('.')
        tf = self._get_target_format(source_format).lower().lstrip('.')
        cfg = self.conv_config

        if sf == tf:
            # 只调大小/质量/尺寸，不改变格式
            has_fit = (cfg.max_size_bytes or cfg.target_dimensions)
            if has_fit:
                return fit_size(
                    data, sf,
                    max_size_bytes=cfg.max_size_bytes,
                    target_dimensions=cfg.target_dimensions,
                    timeout_seconds=cfg.timeout_seconds,
                )
            return ConvertResult(
                success=True, content=data, format=sf, source_format=sf,
                meta={"note": "同格式无操作，直接透传"},
            )

        return convert(data, sf, tf, config=cfg)

    async def _convert_data_group(
        self, files: list[FileItem], source_format: str
    ) -> list[tuple[FileItem, ConvertResult]]:
        """DATA 类型同组：解析各行 JSON → 合并 DataFrame → 一次转换 → 拆分结果。

        特殊：同格式只调大小时回退到逐文件 fit_size()，避免无意义的 DataFrame 来回。
        """
        target_format = self._get_target_format(source_format)
        if source_format == target_format and self.conv_config.max_size_bytes:
            return await self._convert_binary_group(files, source_format)

        import pandas as pd

        results: list[tuple[FileItem, ConvertResult]] = []
        records: list[dict] = []

        # 解析每个文件为 dict
        for f in files:
            try:
                text = f.data.decode("utf-8", errors="replace").strip()
                try:
                    parsed = json.loads(text)
                    if isinstance(parsed, dict):
                        records.append(parsed)
                    elif isinstance(parsed, list):
                        for item in parsed:
                            if isinstance(item, dict):
                                records.append(item)
                except json.JSONDecodeError:
                    records.append({"content": text})
            except Exception as e:
                results.append((f, ConvertResult(
                    success=False, error=f"解析数据行失败: {e}",
                    source_format=source_format, format=target_format,
                )))

        if not records:
            return results

        # 合并为一个 DataFrame 批量转换
        try:
            df = pd.DataFrame(records)
            content = _write_dataframe(df, target_format)

            cfg = self.conv_config
            sampled_meta: dict = {}
            if cfg.max_size_bytes and len(content) > cfg.max_size_bytes:
                df, content, sampled = _sample_dataframe_to_fit(df, target_format, cfg.max_size_bytes)
                if sampled:
                    sampled_meta = {"sampled": True, "row_count": len(df)}

            # 批量成功 → 每个文件标记成功，共享同一个输出 content
            for f in files:
                results.append((f, ConvertResult(
                    success=True, content=content,
                    format=target_format, source_format=source_format,
                    meta={"row_count": len(df), "column_count": len(df.columns),
                          "batch_converted": True, **sampled_meta},
                )))
        except Exception as e:
            await self._log("warning", f"DataFrame 批量转换失败 ({e})，回退到逐文件转换")
            return await self._convert_binary_group(files, source_format, target_format)

        return results

    async def _convert_binary_group(
        self, files: list[FileItem], source_format: str,
        target_format: str | None = None,
    ) -> list[tuple[FileItem, ConvertResult]]:
        """Binary / 混合类型组：每个文件独立 convert()，asyncio.gather 并发。"""
        sem = self._semaphore

        async def convert_one(f: FileItem) -> tuple[FileItem, ConvertResult]:
            async with sem:
                try:
                    sf = f.source_format or source_format
                    result = await self._run_in_thread(
                        self._apply_conversion, f.data, sf)
                    return f, result
                except Exception as e:
                    return f, ConvertResult(
                        success=False, error=f"异步转换异常: {e}",
                        source_format=f.source_format or source_format,
                        format=target_format or self.target_format,
                    )

        tasks = [convert_one(f) for f in files]
        gathered = await asyncio.gather(*tasks)
        return list(gathered)

    # ════════════════════════════════════════════════════════════════════════
    # 读取上游数据 — MySQL 路径（结构化数据）
    # ════════════════════════════════════════════════════════════════════════

    async def load_previous_node_data(self) -> tuple[list[FileItem], list[dict]]:
        """从上游节点读取数据，返回 (内联FileItem列表, MinIO引用列表)。

        内联数据已在内存（来自 MySQL content_b64），MinIO 引用只有元数据。
        调用方对流式处理方法 _stream_minio_pipeline() 逐个下载→转换→保存。
        """
        if self._upstream is not None:
            # 自定义上游直接返回 FileItem（都已加载到内存，无 MinIO 引用）
            items = await self._upstream.fetch(
                pre_node=self.pre_node,
                flow_id=self.flow_id,
                exec_version=self.exec_version,
            )
            return items, []

        if self._minio_source is not None:
            data = await self._minio_source.fetch_bytes(
                bucket=self._get_minio_bucket(),
                object_key=self._resolve_minio_key(),
            )
            fmt = self._source_format_val or self._resolve_minio_key().rsplit(".", 1)[-1]
            return [FileItem(
                name=self._resolve_minio_key(),
                data=data,
                source_format=fmt.lower().lstrip("."),
            )], []

        return await self._load_upstream()

    async def _load_upstream(self) -> tuple[list[FileItem], list[dict]]:
        """读取上游节点数据，返回 (内联数据, MinIO引用)。

        - 内联数据（content_b64 / 纯文本）→ 直接构造 FileItem，bytes 在内存
        - MinIO 引用（path）→ 只返回元数据 dict，不下载。由 _stream_minio_pipeline 流式处理

        MinIO ref 格式: {name, bucket, object_key, format}
        """
        if self.db is None or self.db_config is None:
            raise RuntimeError(
                "未提供 db/db_config，无法自动读取上游数据。"
                "请传入 upstream 参数或提供数据库连接。"
            )

        source_table = self._upstream_meta.get("source_table", "")
        if not source_table:
            raise RuntimeError(
                "context['upstream_meta']['source_table'] 未提供，"
                "无法定位上游数据表。请检查框架层是否正确传入了上游元信息。"
            )
        source_table = _validate_identifier(source_table, max_len=128)

        await self._log("info", f"读取上游表: {source_table}")

        try:
            from sqlalchemy import text
            sql = text(f"SELECT data FROM `{source_table}`")
            result = await self.db.execute(sql)
            rows = [dict(r._mapping) for r in result.fetchall()]
        except Exception as e:
            raise RuntimeError(f"读取上游表 {source_table} 失败: {e}")

        if not rows:
            return [], []

        inline_items: list[FileItem] = []
        minio_refs: list[dict] = []

        for i, row in enumerate(rows):
            data_val = row.get("data", row)
            if isinstance(data_val, str):
                try:
                    data_val = json.loads(data_val)
                except (json.JSONDecodeError, TypeError):
                    inline_items.append(FileItem(
                        name=f"row_{i + 1}", row_id=row.get("id", i + 1),
                        data=data_val.encode("utf-8") if isinstance(data_val, str) else b"",
                        source_format="txt",
                    ))
                    continue

            if not isinstance(data_val, dict):
                inline_items.append(FileItem(
                    name=f"row_{i + 1}", row_id=row.get("id", i + 1),
                    data=str(data_val).encode("utf-8"), source_format="txt",
                ))
                continue

            path = data_val.get("path", "")
            if path:
                # MinIO 引用 → 只记元数据，不下
                fmt = (data_val.get("_format", "")
                       or data_val.get("format", ""))
                if not fmt and "." in path:
                    ext = path.rsplit(".", 1)[-1].split("?")[0]
                    if len(ext) <= 6:
                        fmt = ext
                if not fmt:
                    fmt = self._source_format_val or "bin"

                if "/" in path:
                    bucket, object_key = path.split("/", 1)
                else:
                    bucket = self._get_minio_bucket()
                    object_key = path

                name = (data_val.get("_name", "")
                        or data_val.get("name", "")
                        or object_key.rsplit("/", 1)[-1])
                minio_refs.append({
                    "name": name,
                    "bucket": bucket,
                    "object_key": object_key,
                    "format": fmt.lower().lstrip("."),
                })
            else:
                # 内联数据 → 提取实际内容
                content_b64 = data_val.get("content_b64", "")
                inner_data = data_val.get("data")  # 旧 converter 格式

                if content_b64:
                    try:
                        file_data = base64.b64decode(content_b64)
                    except Exception:
                        file_data = json.dumps(data_val, ensure_ascii=False).encode("utf-8")
                elif inner_data is not None:
                    if isinstance(inner_data, (list, dict)):
                        file_data = json.dumps(inner_data, ensure_ascii=False).encode("utf-8")
                    else:
                        file_data = str(inner_data).encode("utf-8")
                else:
                    # 新格式 / DataStorage 原始：直接就是业务记录
                    file_data = json.dumps(data_val, ensure_ascii=False).encode("utf-8")

                # 格式：新格式用 _format，旧格式用 format/source_format，都没有用全局默认
                fmt = (data_val.get("_format", "")
                       or data_val.get("format", "")
                       or data_val.get("source_format", "")
                       or self._source_format_val
                       or "json")
                name = data_val.get("_name", "") or data_val.get("name", "") or f"row_{i + 1}.{fmt}"
                inline_items.append(FileItem(
                    name=name, data=file_data,
                    source_format=fmt.lower().lstrip("."),
                    row_id=row.get("id", i + 1),
                ))

        await self._log(
            "info",
            f"上游数据解析完成：内联 {len(inline_items)} 行, MinIO 引用 {len(minio_refs)} 个",
        )
        return inline_items, minio_refs


    # ════════════════════════════════════════════════════════════════════════
    # 读取上游数据 — MinIO 路径（非结构化数据）
    # ════════════════════════════════════════════════════════════════════════

    def _get_minio_bucket(self) -> str:
        """获取 MinIO bucket 名称。"""
        return self._minio_config.get("minio_bucket", "")

    def _resolve_minio_key(self) -> str:
        """拼接 MinIO 对象 key。"""
        prefix = self.minio_rule.get("prefix", "")
        return f"{prefix}input.{self._source_format_val}"

    # ════════════════════════════════════════════════════════════════════════
    # 上游数据概览（前端转换页展示用）
    # ════════════════════════════════════════════════════════════════════════

    async def get_upstream_summary(self) -> dict[str, Any]:
        """扫描上游版本表，返回格式分布、文件大小统计。

        不下载 MinIO 文件内容，只读元数据。前端用此数据画饼图+树图。
        在 run() 之前调用，帮助用户在转换页面上配置参数。
        """
        source_table = self._upstream_meta.get("source_table", "")
        if not source_table:
            return {"error": "未配置上游表", "total_files": 0, "total_size": 0}
        source_table = _validate_identifier(source_table, max_len=128)

        try:
            from sqlalchemy import text
            sql = text(f"SELECT data FROM `{source_table}`")
            result = await self.db.execute(sql)
            rows = result.fetchall()
        except Exception as e:
            return {"error": f"查询上游表失败: {e}", "total_files": 0, "total_size": 0}

        if not rows:
            return {"total_files": 0, "total_size": 0, "by_format": {}, "files": []}

        # 逐行解析元数据
        stats: dict[str, dict] = {}  # format → {count, total_size, sizes: [...]}
        files: list[dict] = []

        for row in rows:
            data_val = row[0]
            if isinstance(data_val, str):
                try:
                    data_val = json.loads(data_val)
                except (json.JSONDecodeError, TypeError):
                    continue
            if not isinstance(data_val, dict):
                continue

            # 格式: format 字段 > path 后缀 > "unknown"
            fmt = (data_val.get("_format", "")
                   or data_val.get("format", "")
                   or data_val.get("source_format", ""))
            path = data_val.get("path", "")
            if not fmt and path and "." in path:
                ext = path.rsplit(".", 1)[-1].split("?")[0]
                if len(ext) <= 6:
                    fmt = ext
            if not fmt:
                fmt = "unknown"

            # 大小: _size > size > content_b64 长度 > 0
            size = (data_val.get("_size")
                    or data_val.get("size"))
            if size is None and data_val.get("content_b64"):
                size = len(data_val["content_b64"]) * 3 // 4  # base64 解码后近似
            size = int(size or 0)

            name = (data_val.get("_name", "")
                    or data_val.get("name", "")
                    or f"item_{len(files) + 1}")

            if fmt not in stats:
                stats[fmt] = {"count": 0, "total_size": 0, "sizes": []}
            stats[fmt]["count"] += 1
            stats[fmt]["total_size"] += size
            stats[fmt]["sizes"].append(size)

            files.append({"name": name, "format": fmt, "size": size,
                          "path": path})

        # MinIO 行没有 size 的 → HEAD 请求获取文件大小（不下载内容）
        pending_stat = [(f, f["path"]) for f in files if f["size"] == 0 and f.get("path")]
        if pending_stat and self._minio_client is not None:
            for f, path in pending_stat:
                try:
                    if "/" in path:
                        bucket, key = path.split("/", 1)
                    else:
                        bucket, key = self._get_minio_bucket(), path
                    obj_info = await self._minio_client.stat_object(bucket, key)
                    f["size"] = obj_info.size if hasattr(obj_info, 'size') else (obj_info.get("size", 0) if isinstance(obj_info, dict) else 0)
                except Exception:
                    pass  # stat 失败保留 0

        # 更新 stats（因为 MinIO size 可能变了）
        stats.clear()
        for f in files:
            fmt = f["format"]
            if fmt not in stats:
                stats[fmt] = {"count": 0, "total_size": 0, "sizes": []}
            stats[fmt]["count"] += 1
            stats[fmt]["total_size"] += f["size"]
            stats[fmt]["sizes"].append(f["size"])

        # 聚合每个格式的统计
        total_size = 0
        by_format = {}
        for fmt, st in stats.items():
            sizes = sorted(st["sizes"])
            n = len(sizes)
            total_size += st["total_size"]
            by_format[fmt] = {
                "count": st["count"],
                "total_size": st["total_size"],
                "min_size": sizes[0] if n > 0 else 0,
                "max_size": sizes[-1] if n > 0 else 0,
                "median_size": sizes[n // 2] if n > 0 else 0,
                "p80_low": sizes[max(0, int(n * 0.1))] if n > 0 else 0,
                "p80_high": sizes[min(n - 1, int(n * 0.9))] if n > 0 else 0,
            }

        return {
            "total_files": len(files),
            "total_size": total_size,
            "by_format": by_format,
            "files": files,
        }

    # ════════════════════════════════════════════════════════════════════════
    # 保存结果
    # ════════════════════════════════════════════════════════════════════════

    async def _save_results(self, results: list[tuple[FileItem, ConvertResult]]) -> None:
        """批量保存转换结果，自动分流大文件。

        - ≤10MB: 直接存 MySQL（内联数据或 base64）
        - >10MB + MinIO 可用: 自动走 MinIO，MySQL 只记路径
        - >10MB + MinIO 不可用: base64 塞 MySQL
        """
        _LARGE_FILE = 10 * 1024 * 1024  # 10MB 阈值

        for target in self.target_type:
            if target == "minio":
                await self._save_to_minio_batch(results)
            elif target == "mysql":
                # 分流：大文件 + MinIO 可用 → 自动走 MinIO
                mysql_results = []
                minio_overflow = []
                for fi, r in results:
                    if r.success and r.content and len(r.content) > _LARGE_FILE \
                       and self._minio_client is not None:
                        minio_overflow.append((fi, r))
                    else:
                        mysql_results.append((fi, r))
                if mysql_results:
                    await self._save_to_mysql_batch(mysql_results)
                if minio_overflow:
                    await self._log("info",
                        f"{len(minio_overflow)} 个文件 >10MB，自动走 MinIO")
                    await self._save_to_minio_batch(minio_overflow)
            else:
                await self._log("warning", f"不支持的目标类型: {target}，跳过")

    # ── MySQL 批量写回 ──────────────────────────────────────────────────


    async def _save_to_mysql_batch(
        self, results: list[tuple[FileItem, ConvertResult]]
    ) -> None:
        """批量 INSERT 到版本表，分块写入（batch_size=500）。"""
        if self.db is None:
            return

        fail_count = sum(1 for _, r in results if not r.success)
        success_rows = await self._run_in_thread(
            _serialize_results, results)
        if not success_rows:
            await self._log("info", f"无成功结果需要写入 MySQL，失败 {fail_count}")
            return

        BATCH = 500
        inserted = 0
        try:
            from sqlalchemy import text
            for chunk_start in range(0, len(success_rows), BATCH):
                chunk = success_rows[chunk_start:chunk_start + BATCH]
                chunk_ids: list[str] = []

                placeholders = ", ".join(
                    f"(:c{i}, :d{i}_id, :d{i}, 'pending')"
                    for i in range(len(chunk))
                )
                params = {}
                for i in range(len(chunk)):
                    row = json.loads(chunk[i])
                    cid = row.get("_name", row.get("name", f"item_{chunk_start + i}"))
                    chunk_ids.append(cid)
                    params[f"c{i}"] = cid
                    params[f"d{i}_id"] = cid
                    params[f"d{i}"] = chunk[i]

                # ① INSERT: status='pending'
                sql = text(
                    f"INSERT INTO `{self.result_table}` "
                    f"(chunk_id, data_id, data, status) VALUES {placeholders}"
                )
                await self.db.execute(sql, params)

                # ② UPDATE: status='success'（对齐 DataStorage 逐块提交）
                cid_placeholders = ", ".join(f":cid{i}" for i in range(len(chunk_ids)))
                update_sql = text(
                    f"UPDATE `{self.result_table}` "
                    f"SET status = 'success' WHERE chunk_id IN ({cid_placeholders})"
                )
                await self.db.execute(
                    update_sql,
                    {f"cid{i}": chunk_ids[i] for i in range(len(chunk_ids))},
                )
                inserted += len(chunk)

            await self.db.commit()
        except Exception as e:
            await self._log("error", f"MySQL 批量写入失败: {e}")
            raise

        await self._log(
            "info",
            f"转换结果已写入 MySQL {self.result_table}：成功 {inserted}，失败 {fail_count}",
        )

    # ── MinIO 批量写回 ──────────────────────────────────────────────────

    async def _save_to_minio_batch(
        self, results: list[tuple[FileItem, ConvertResult]]
    ) -> None:
        """每个文件上传一个 MinIO 对象。

        对象命名：{prefix}{flow_id}_v{version}_{filename}.{format}

        MinIO 路径的 MySQL 记录收集后批量 INSERT。
        """
        if self._minio_client is None:
            await self._log("warning", "未提供 minio_client，跳过 MinIO 写入")
            return

        bucket = self._get_minio_bucket()
        if not bucket:
            raise RuntimeError("未配置 minio_bucket，无法写入 MinIO")

        prefix = self.minio_rule.get("prefix", "converted/")
        flow_safe = self.flow_id.replace("-", "_")

        # Step 1: 逐个上传 MinIO，收集 MySQL 路径记录
        success_count = 0
        fail_count = 0
        mysql_path_rows: list[str] = []  # 批量 INSERT 用

        for fi, result in results:
            if not result.success:
                continue

            base_name = fi.name.rsplit(".", 1)[0] if "." in fi.name else fi.name
            object_name = f"{prefix}{flow_safe}_v{self.exec_version}_{base_name}.{result.format}"

            file_size = len(result.content)
            try:
                if file_size > 10 * 1024 * 1024:
                    await self._minio_client.upload_multipart(bucket, object_name, result.content)
                else:
                    await self._minio_client.upload_data(bucket, object_name, result.content)

                minio_path = f"{bucket}/{object_name}"
                success_count += 1

                # 记录到待批量写入列表（下划线前缀 = 元数据字段，避免跟业务字段冲突）
                if "mysql" in self.target_type and self.db is not None:
                    mysql_path_rows.append(json.dumps({
                        "_format": result.format,
                        "_source_format": result.source_format or fi.source_format,
                        "path": minio_path,
                        "_name": fi.name,
                        "_size": file_size,
                    }, ensure_ascii=False))

            except Exception as e:
                fail_count += 1
                await self._log("error", f"写入 MinIO 失败 [{fi.name}]: {e}")

        # Step 2: 批量 INSERT MinIO 路径到 MySQL（对齐 chunk 结构）
        # MinIO 上传成功后，MySQL 记录写入失败 → 标记对应文件为 failed
        if mysql_path_rows and self.db is not None:
            BATCH = 500
            try:
                from sqlalchemy import text
                for chunk_start in range(0, len(mysql_path_rows), BATCH):
                    chunk = mysql_path_rows[chunk_start:chunk_start + BATCH]
                    chunk_ids: list[str] = []
                    placeholders = ", ".join(
                        f"(:c{i}, :d{i}_id, :d{i}, 'pending')"
                        for i in range(len(chunk))
                    )
                    params = {}
                    for i in range(len(chunk)):
                        row = json.loads(chunk[i])
                        cid = row.get("_name", f"minio_{chunk_start + i}")
                        chunk_ids.append(cid)
                        params[f"c{i}"] = cid
                        params[f"d{i}_id"] = cid
                        params[f"d{i}"] = chunk[i]

                    sql = text(
                        f"INSERT INTO `{self.result_table}` "
                        f"(chunk_id, data_id, data, status) VALUES {placeholders}"
                    )
                    await self.db.execute(sql, params)

                    # UPDATE → success
                    cid_phs = ", ".join(f":cid{i}" for i in range(len(chunk_ids)))
                    await self.db.execute(
                        text(f"UPDATE `{self.result_table}` "
                             f"SET status = 'success' WHERE chunk_id IN ({cid_phs})"),
                        {f"cid{i}": chunk_ids[i] for i in range(len(chunk_ids))},
                    )
                await self.db.commit()
            except Exception as e:
                # MySQL 路径记录写入失败 → MinIO 文件成孤儿
                # 将这批的 success_count 回退，标记为失败
                await self._log("error", f"MinIO 路径批量写入 MySQL 失败（{len(mysql_path_rows)} 条记录丢失）: {e}")
                fail_count += len(mysql_path_rows)
                success_count -= len(mysql_path_rows)

        await self._log(
            "info",
            f"转换结果已写入 MinIO {bucket}/{prefix}*：成功 {success_count}，失败 {fail_count}",
        )

    # ════════════════════════════════════════════════════════════════════════
    # 表管理 & 工具方法
    # ════════════════════════════════════════════════════════════════════════

    async def _load_history_failed_files(self) -> list[str]:
        """从上一版本表查询失败/挂起的 chunk_id（对齐 DataStorage 断点续跑）。

        同时检查 failed 和 pending 状态——pending 意味着上次写入中途崩溃。
        """
        if not self.last_version_table or self.db is None:
            return []

        # 校验表名防止注入
        last_tbl = _validate_identifier(self.last_version_table, max_len=128)

        try:
            from sqlalchemy import text
            # 检查上一版本表是否存在（用参数化查询防注入）
            check_sql = text(
                "SELECT COUNT(*) FROM information_schema.tables "
                "WHERE table_schema = DATABASE() AND table_name = :tbl"
            )
            result = await self.db.execute(check_sql, {"tbl": last_tbl})
            if result.fetchone()[0] == 0:
                return []

            # 查询失败和挂起的 chunk（pending = 上次写入中途崩溃）
            sql = text(
                f"SELECT DISTINCT chunk_id FROM `{last_tbl}` "
                f"WHERE status IN ('failed', 'pending')"
            )
            result = await self.db.execute(sql)
            failed = [row[0] for row in result.fetchall()]
            if failed:
                await self._log(
                    "info",
                    f"从版本 v{self.last_version} 恢复 {len(failed)} 个失败文件: {failed}",
                )
            return failed
        except Exception as e:
            await self._log("warning", f"查询历史失败文件失败: {e}")
            return []

    async def _create_result_table(self) -> None:
        """创建当前版本的 MySQL 结果表（对齐 DataStorage 的 chunk 结构）。

        参照 DataStorage.create_storage_version_table：
        - chunk_id: 分块 ID（converter 每个文件一个 chunk）
        - data_id:  数据行 ID（同 chunk_id，每个文件一行）
        - status:   raw / pending / success / failed —— 支持断点续跑
        """
        if self.db is None:
            return

        try:
            from sqlalchemy import text
            create_sql = text(f"""
                CREATE TABLE IF NOT EXISTS `{self.result_table}` (
                    id BIGINT AUTO_INCREMENT PRIMARY KEY,
                    chunk_id VARCHAR(256) NOT NULL COMMENT '分块ID=文件名，同文件多版本共享',
                    data_id VARCHAR(256) NOT NULL COMMENT '数据行ID=文件名',
                    data JSON NOT NULL COMMENT '转换结果',
                    status VARCHAR(32) DEFAULT 'raw' COMMENT 'raw/pending/success/failed',
                    created_at DATETIME DEFAULT NOW(),
                    INDEX idx_chunk_id (chunk_id),
                    INDEX idx_status (status)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                COMMENT='converter_core 节点输出表 - flow={self.flow_id} v{self.exec_version}';
            """)
            await self.db.execute(create_sql)
            await self.db.commit()
        except Exception as e:
            await self._log("warning", f"创建结果表 {self.result_table} 失败（可能已存在）: {e}")

    async def _log(self, level: str, msg: str) -> None:
        """统一日志输出 —— 优先使用注入的 logger，否则 print。"""
        if self._logger is not None:
            await self._logger(level, f"[ConverterNode | {self.flow_id}] {msg}")
        else:
            print(f"[ConverterNode | {level}] [{self.flow_id}] {msg}")

    async def close(self) -> None:
        """释放资源：关闭线程池。框架应在节点完成后调用。"""
        if self._executor is not None:
            self._executor.shutdown(wait=False, cancel_futures=True)
            self._executor = None
        await self._log("info", "ConverterNode 资源已释放")


# ============================================================================
# API 参考 
# ============================================================================
#
# 依赖注入（全局或通过 Depends）:
#   db           = SQLAlchemy async session
#   db_config    = 数据库连接配置 dict
#   minio_client = AsyncMinIOClient 实例
#   minio_config = {minio_host, minio_port, minio_user, minio_password, minio_bucket}
#   flow_dag     = 完整流程 DAG 配置 dict（所有节点的 config 映射）
#
# ════════════════════════════════════════════════════════════════════════════
#
# 1. 上游格式概览（饼图 + 树图数据）
# ────────────────────────────────────────
# GET /api/flow/{flow_id}/upstream-summary?pre_node=data_collector&exec_version=1
#
# @router.get("/api/flow/{flow_id}/upstream-summary")
# async def upstream_summary(
#     flow_id: str, pre_node: str, exec_version: int = 1,
#     db=Depends(get_db), flow_dag=Depends(get_flow_config),
# ):
#     node = ConverterNode(
#         pre_node=pre_node, flow_id=flow_id,
#         config={},
#         context={
#             "exec_version": exec_version,
#             "upstream_meta": {
#                 "source_table": f"flow_{flow_id}_{pre_node}_v{exec_version}",
#             },
#         },
#         db=db, flow_config=flow_dag,
#     )
#     return await node.get_upstream_summary()
#
#
# 2. 可选目标格式列表（前端下拉框）
# ────────────────────────────────────────
# GET /api/formats?ext=csv
#
# @router.get("/api/formats")
# def get_target_formats(ext: str):
#     ft = detect_type(f".{ext}")
#     return {"type": ft.value, "targets": get_formats(ft)}
#
#
# 3. 执行转换
# ────────────────────────────────────────
# POST /api/flow/{flow_id}/convert
# Body: {
#   "pre_node":           "data_collector",       # 必填: 上游节点名
#   "target_format":      "json",                 # 必填: 输出格式
#   "source_format_filter": ["csv","json"],       # 可选: 只转换指定格式，不填=全部
#   "target_format_map":  {"csv":"json","jpg":"webp"}, # 可选: 按源格式指定目标，优先级高于target_format
#   "converter_config": {                         # 可选: 转换参数
#     "max_size_bytes":    1048576,               #   文件大小上限(字节)
#     "target_dimensions": [1920, 1080],          #   图像统一尺寸(宽,高)
#     "quality":           85,                    #   图像/视频质量 1-100, 默认95
#     "timeout_seconds":   300                    #   超时, 默认300
#   },
#   "exec_version":       1,                      # 可选: 版本号, 默认1
#   "concurrency":        5,                      # 可选: 并发数, 默认5
#   "max_retries":        3                       # 可选: 重试次数, 默认3
# }
#
# @router.post("/api/flow/{flow_id}/convert")
# async def execute_conversion(
#     flow_id: str, body: dict,
#     db=Depends(get_db), db_config=Depends(get_db_config),
#     minio_client=Depends(get_minio_client), minio_config=Depends(get_minio_config),
#     flow_dag=Depends(get_flow_config),
# ):
#     node = ConverterNode(
#         pre_node=body["pre_node"], flow_id=flow_id,
#         config={
#             "source_format_filter": body.get("source_format_filter"),
#             "target_format":        body["target_format"],
#             "converter_config":     body.get("converter_config", {}),
#             "concurrency":          body.get("concurrency", 5),
#             "max_retries":          body.get("max_retries", 3),
#         },
#         context={
#             "exec_version": body.get("exec_version", 1),
#             "upstream_meta": {
#                 "source_table": (
#                     f"flow_{flow_id}_{body['pre_node']}"
#                     f"_v{body.get('exec_version', 1)}"
#                 ),
#             },
#         },
#         db=db, db_config=db_config,
#         minio_client=minio_client, minio_config=minio_config,
#         flow_config=flow_dag,
#     )
#     return await node.run()

