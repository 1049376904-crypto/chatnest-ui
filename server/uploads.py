"""上传落盘。跟上游同一套路径校验，但体积限得多。

上游允许 60MB，因为那边模型用 Read 工具自己去看硬盘上的文件。
这边没有工具，附件得跋回 base64 塞进请求体——图片 8MB 就已经能
把中转站的请求体上限顶穿，所以压到 12MB。
"""

import base64
import mimetypes
import re
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path

from fastapi import HTTPException, UploadFile

from server.config import UPLOAD_ROOT

MAX_FILE_BYTES = 12 * 1024 * 1024
MAX_FILES = 10
MAX_TEXT_INLINE_CHARS = 60_000

IMAGE_EXTENSIONS = {".bmp", ".gif", ".jpeg", ".jpg", ".png", ".webp"}
TEXT_EXTENSIONS = {
    ".c", ".cc", ".cpp", ".css", ".csv", ".go", ".h", ".hpp", ".html",
    ".ini", ".java", ".js", ".json", ".jsx", ".log", ".md", ".php",
    ".ps1", ".py", ".rb", ".rs", ".scss", ".sh", ".sql", ".text",
    ".toml", ".ts", ".tsx", ".txt", ".xml", ".yaml", ".yml",
}
# heic/heif 和 pdf 存得下、也能下载，但没法拼进 chat completions 请求。
ALLOWED_EXTENSIONS = IMAGE_EXTENSIONS | TEXT_EXTENSIONS | {".heic", ".heif", ".pdf"}


def _inside(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def _safe_name(filename: str) -> str:
    original = Path(filename or "file").name
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", original).strip("._")
    return cleaned[:160] or "file"


def _safe_conv_dir(conv_id: str) -> Path:
    # conv_id 是自己生的 uuid，但它从请求体里来，不能当安全值用。
        target = (UPLOAD_ROOT / Path(conv_id).name).resolve()
        if not _inside(target, UPLOAD_ROOT):
            raise HTTPException(status_code=400, detail="无效会话路径")
        return target


def attachment_metadata(path: Path) -> dict:
    mime, _ = mimetypes.guess_type(path.name)
    suffix = path.suffix.lower()
    return {
        "name": path.name.split("_", 2)[-1],
        "path": str(path.resolve()),
        "mime": mime or "application/octet-stream",
        "size": path.stat().st_size,
        "is_image": suffix in IMAGE_EXTENSIONS,
    }


async def save_uploads(conv_id: str, files: list[UploadFile]) -> list[dict]:
    if not files or len(files) > MAX_FILES:
        raise HTTPException(status_code=400, detail=f"请选择 1 到 {MAX_FILES} 个文件")
    target_dir = _safe_conv_dir(conv_id)
    target_dir.mkdir(parents=True, exist_ok=True)
    saved: list[dict] = []
    created: list[Path] = []
    try:
        for upload in files:
            suffix = Path(upload.filename or "").suffix.lower()
            if suffix not in ALLOWED_EXTENSIONS:
                raise HTTPException(
                    status_code=415,
                    detail=f"不支持的文件类型：{upload.filename}",
                )
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
            filename = f"{stamp}_{uuid.uuid4().hex[:8]}_{_safe_name(upload.filename)}"
            target = (target_dir / filename).resolve()
            if not _inside(target, target_dir):
                raise HTTPException(status_code=400, detail="无效文件名")
            size = 0
            try:
                with target.open("wb") as output:
                    while chunk := await upload.read(1024 * 1024):
                        size += len(chunk)
                        if size > MAX_FILE_BYTES:
                            raise HTTPException(
                                status_code=413,
                                detail=f"{upload.filename} 超过 12MB",
                            )
                        output.write(chunk)
                created.append(target)
                saved.append(attachment_metadata(target))
            finally:
                await upload.close()
    except Exception:
        for path in created:
            path.unlink(missing_ok=True)
        raise
    return saved


def validated_attachments(conv_id: str, paths: list[str]) -> list[dict]:
    if len(paths) > MAX_FILES:
        raise HTTPException(status_code=400, detail="附件数量过多")
    conversation_root = _safe_conv_dir(conv_id)
    result = []
    for raw_path in paths:
        path = Path(raw_path).resolve()
        if not _inside(path, conversation_root) or not path.is_file():
            raise HTTPException(status_code=400, detail="附件路径无效")
        result.append(attachment_metadata(path))
    return result


def validated_file(conv_id: str, filename: str) -> Path:
    directory = _safe_conv_dir(conv_id)
    path = (directory / Path(filename).name).resolve()
    if not _inside(path, directory) or not path.is_file():
        raise HTTPException(status_code=404, detail="文件不存在")
    return path


def remove_conversation_uploads(conv_id: str) -> None:
    try:
        target = _safe_conv_dir(conv_id)
    except HTTPException:
        return
    if target.is_dir():
        shutil.rmtree(target, ignore_errors=True)


def as_data_url(path: Path, mime: str) -> str:
    encoded = base64.b64encode(path.read_bytes()).decode()
    return f"data:{mime};base64,{encoded}"


def inline_text(path: Path) -> str:
    """文本附件直接读成字符串拼进 prompt。读不出来就算了，不要报错。"""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    if len(text) > MAX_TEXT_INLINE_CHARS:
        text = text[:MAX_TEXT_INLINE_CHARS] + "\n\n[文件过长，已截断]"
    return text
