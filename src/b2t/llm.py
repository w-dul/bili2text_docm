from __future__ import annotations

import json
import re
import socket
import time
import urllib.error
import urllib.request
from typing import Callable

from b2t.segments import segment_text, visible_char_count
from b2t.user_config import LLMConfig

MAX_CHUNK_CHARS = 1500
MAX_LENGTH_DRIFT = 0.15

_SENTENCE_ENDERS = set("。！？；…!?;\n")
_FENCE_PATTERN = re.compile(r"^```[A-Za-z0-9_-]*\s*\n?(.*?)\n?```$", re.DOTALL)
_PREFIXES = (
    "修正后的文本：",
    "修正后的文本:",
    "修正后：",
    "修正后:",
    "修正结果：",
    "以下是修正后的文本：",
    "以下是修正后的文本:",
    "以下是修正结果：",
    "以下是修正结果:",
)

SYSTEM_PROMPT = (
    "你是中文逐字稿校对助手。只修正错别字、同音字、英文大小写、断句与标点。"
    "不得总结、扩写、改写文风、删减任何句子，必须保持原意、段落和顺序。"
    "保持原文的换行与段落结构，不要新增或删除换行。"
    "只输出修正后的纯文本，不要解释，不要使用 Markdown 代码块或任何包裹。"
)


class LLMError(RuntimeError):
    pass


class LLMCancelled(RuntimeError):
    pass


def split_for_llm(text: str, max_chars: int = MAX_CHUNK_CHARS) -> list[str]:
    """Split text into chunks, preserving every character across chunks."""
    if len(text) <= max_chars:
        return [text] if text else []

    chunks: list[str] = []
    remaining = text
    while len(remaining) > max_chars:
        window = remaining[:max_chars]
        minimum = int(max_chars * 0.5)
        split_at = 0
        for index, char in enumerate(window):
            if index >= minimum and char in _SENTENCE_ENDERS:
                split_at = index + 1
        if split_at == 0:
            newline = window.rfind("\n")
            if newline >= minimum:
                split_at = newline + 1
        if split_at == 0:
            split_at = max_chars
        chunks.append(remaining[:split_at])
        remaining = remaining[split_at:]
    if remaining:
        chunks.append(remaining)
    return chunks


class LLMClient:
    def __init__(self, config: LLMConfig) -> None:
        self.config = config

    def correct(
        self,
        text: str,
        *,
        on_progress: Callable[[int, int], None] | None = None,
        is_cancelled: Callable[[], bool] | None = None,
    ) -> str:
        chunks = split_for_llm(text)
        if not chunks:
            raise LLMError("没有可修正的文本")
        total = len(chunks)
        corrected_chunks: list[str] = []
        for index, chunk in enumerate(chunks, start=1):
            if is_cancelled is not None and is_cancelled():
                raise LLMCancelled("cancelled")
            if on_progress is not None:
                on_progress(index, total)
            corrected = self._correct_chunk(chunk)
            if not corrected.strip():
                raise LLMError(f"第 {index}/{total} 段返回为空")
            drift = abs(len(corrected) - len(chunk)) / max(1, len(chunk))
            if drift > MAX_LENGTH_DRIFT:
                raise LLMError(f"第 {index}/{total} 段修正后长度异常，差异 {drift:.0%}")
            corrected_chunks.append(corrected)
        result = "".join(corrected_chunks)
        if not result.strip():
            raise LLMError("修正结果为空")
        drift = abs(len(result) - len(text)) / max(1, len(text))
        if drift > MAX_LENGTH_DRIFT:
            raise LLMError(f"修正后长度异常，差异 {drift:.0%}")
        return format_paragraphs(result)

    def test_connection(self) -> tuple[bool, str, float]:
        started = time.monotonic()
        try:
            self._request(
                [
                    {"role": "system", "content": "你是一个用于连接测试的助手，只输出用户要求的文字。"},
                    {"role": "user", "content": "请原样输出：今天天气很好，连接测试成功。"},
                ]
            )
        except LLMError as exc:
            return False, str(exc), time.monotonic() - started
        return True, "连接成功", time.monotonic() - started

    def _correct_chunk(self, chunk: str) -> str:
        system_prompt = SYSTEM_PROMPT
        glossary = self.config.glossary.strip()
        if glossary:
            system_prompt += (
                "\n\n术语表（每行一条“错误写法 → 正确写法”，出现时必须替换）：\n" + glossary
            )
        content = self._request(
            [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": chunk},
            ]
        )
        return _unwrap(content)

    def _request(self, messages: list[dict[str, str]]) -> str:
        if not self.config.base_url or not self.config.api_key or not self.config.model:
            raise LLMError("未配置 LLM")

        endpoint = self.config.base_url.rstrip("/")
        if not endpoint.endswith("/chat/completions"):
            endpoint = f"{endpoint}/chat/completions"
        payload = json.dumps(
            {
                "model": self.config.model,
                "messages": messages,
                "temperature": 0,
            },
            ensure_ascii=False,
        ).encode("utf-8")
        request = urllib.request.Request(
            endpoint,
            data=payload,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.config.api_key}",
            },
            method="POST",
        )
        timeout = max(10, min(300, int(self.config.timeout_seconds)))
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                raw = response.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", errors="replace")[:300]
            except Exception:  # noqa: BLE001
                detail = ""
            raise LLMError(f"请求失败（HTTP {exc.code}）{detail}".strip()) from exc
        except (socket.timeout, TimeoutError) as exc:
            raise LLMError(f"请求超时（{timeout}s）") from exc
        except urllib.error.URLError as exc:
            if isinstance(exc.reason, (socket.timeout, TimeoutError)):
                raise LLMError(f"请求超时（{timeout}s）") from exc
            raise LLMError(f"无法连接 LLM 服务：{exc.reason}") from exc

        try:
            data = json.loads(raw)
            content = data["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise LLMError("返回内容不符合纯文本要求") from exc
        if not isinstance(content, str):
            raise LLMError("返回内容不符合纯文本要求")
        return content


def format_paragraphs(text: str, *, max_chars: int = 300) -> str:
    """Ensure every block of the corrected text is paragraph-sized.

    Blocks longer than ``max_chars`` are split with the segmentation rules and
    re-joined with blank lines, so the tail of a long transcription is never a
    single wall of text. The function is idempotent.
    """
    if not text.strip():
        return text
    trailing_newline = text.endswith("\n")
    blocks = re.split(r"\n\s*\n", text.strip("\n"))
    paragraphs: list[str] = []
    for block in blocks:
        block = block.strip()
        if not block:
            continue
        if visible_char_count(block) <= max_chars:
            paragraphs.append(block)
            continue
        paragraphs.extend(
            part.strip() for part in segment_text(block, max_chars=max_chars) if part.strip()
        )
    if not paragraphs:
        return text
    return "\n\n".join(paragraphs) + ("\n" if trailing_newline else "")


def _unwrap(content: str) -> str:
    leading = content[: len(content) - len(content.lstrip())]
    trailing = content[len(content.rstrip()) :]
    text = content.strip()
    fence = _FENCE_PATTERN.match(text)
    if fence:
        text = fence.group(1).strip()
    elif "```" in text:
        blocks = re.findall(r"```[A-Za-z0-9_-]*\s*\n?(.*?)```", text, re.DOTALL)
        if blocks:
            text = max(blocks, key=len).strip()

    if "\n\n" in text:
        head, rest = text.split("\n\n", 1)
        if _looks_explanatory(head) and rest.strip():
            text = rest.strip()

    for prefix in _PREFIXES:
        if text.startswith(prefix):
            text = text[len(prefix):].strip()
            break

    if text.startswith(("好的", "收到")) and "\n" in text:
        head, rest = text.split("\n", 1)
        if rest.strip():
            text = rest.strip()
    return leading + text + trailing


def _looks_explanatory(line: str) -> bool:
    markers = ("修正", "如下", "以下", "好的", "结果", "校对")
    return len(line) <= 40 and any(marker in line for marker in markers)
