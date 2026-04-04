from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import signal
import sys
import time
import ctypes
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterable

import pandas as pd
import requests
import win32api
import win32con
import win32gui
import win32process
from dateutil import parser as date_parser
from dotenv import load_dotenv
from requests import RequestException
from wxautox4 import WeChat


BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")
if not os.getenv("OPENAI_API_KEY"):
    load_dotenv(BASE_DIR / ".env.example", override=False)


LOG_FILE = os.getenv("LOG_FILE", "wxbot.log")
STATE_FILE = BASE_DIR / os.getenv("STATE_FILE", "wxbot_state.json")
WECHAT_TARGET = os.getenv("WECHAT_TARGET", "HAO")
WECHAT_TARGET_ALIASES = [
    item.strip()
    for item in os.getenv("WECHAT_TARGET_ALIASES", "").split(",")
    if item.strip()
]
AKSHARE_SYMBOL = os.getenv("AKSHARE_SYMBOL", "全部")
AKSHARE_NEWS_SOURCES = [
    item.strip().lower()
    for item in os.getenv("AKSHARE_NEWS_SOURCES", "cls,sina,futu,em,ths,cx").split(",")
    if item.strip()
]
POLL_INTERVAL_SECONDS = int(os.getenv("POLL_INTERVAL_SECONDS", "300"))
LOOKBACK_MINUTES = int(os.getenv("LOOKBACK_MINUTES", "6"))
NEWS_HISTORY_LIMIT = int(os.getenv("NEWS_HISTORY_LIMIT", "500"))
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
OPENAI_BASE_URL = os.getenv("OPENAI_BASE_URL", "https://api.siliconflow.cn/v1").rstrip("/")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "Qwen/Qwen2.5-7B-Instruct")
OPENAI_TIMEOUT_SECONDS = int(os.getenv("OPENAI_TIMEOUT_SECONDS", "60"))
OPENAI_MAX_RETRIES = int(os.getenv("OPENAI_MAX_RETRIES", "3"))
MAX_WECHAT_MESSAGE_LEN = int(os.getenv("MAX_WECHAT_MESSAGE_LEN", "1800"))
WECHAT_SEND_MUTEX_NAME = os.getenv("WECHAT_SEND_MUTEX_NAME", "Global\\xinyuelib_wechat_send_mutex")
WECHAT_SEND_LOCK_WAIT_SECONDS = int(os.getenv("WECHAT_SEND_LOCK_WAIT_SECONDS", "10"))
OPENAI_HTTP_PROXY = os.getenv("OPENAI_HTTP_PROXY", "").strip()
OPENAI_HTTPS_PROXY = os.getenv("OPENAI_HTTPS_PROXY", "").strip()
OPENAI_NO_PROXY = os.getenv("OPENAI_NO_PROXY", "").strip()
STARTED_AT = datetime.now()


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] [%(levelname)s] [%(filename)s:%(lineno)d]  %(message)s",
    handlers=[
        logging.FileHandler(BASE_DIR / LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class NewsItem:
    title: str
    content: str
    published_at: datetime
    source: str
    channel: str
    link: str = ""

    @property
    def fingerprint(self) -> str:
        raw = f"{self.channel}|{self.published_at.isoformat()}|{self.title}|{self.content}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    @property
    def dedupe_key(self) -> str:
        normalized = canonicalize_news_text(self.title, self.content)
        return hashlib.sha256(normalized.encode("utf-8")).hexdigest()

    def to_prompt_dict(self) -> dict[str, str]:
        return {
            "title": self.title,
            "content": self.content,
            "published_at": self.published_at.strftime("%Y-%m-%d %H:%M:%S"),
            "source": self.source,
            "channel": self.channel,
            "link": self.link,
        }


class MarketNewsBot:
    def __init__(self) -> None:
        self.wx: WeChat | None = None
        self.started_at = STARTED_AT
        legacy_keys = self._load_state_keys("recent_fingerprints", "sent_fingerprints")
        self.pulled_keys = self._load_state_keys("pulled_fingerprints") or legacy_keys
        self.modeled_keys = self._load_state_keys("modeled_fingerprints") or legacy_keys
        self.sent_keys = self._load_state_keys("sent_fingerprints") or legacy_keys
        self.pulled_key_set = set(self.pulled_keys)
        self.modeled_key_set = set(self.modeled_keys)
        self.sent_key_set = set(self.sent_keys)

    def _load_state_keys(self, primary_key: str, fallback_key: str | None = None) -> list[str]:
        if not STATE_FILE.exists():
            return []
        try:
            payload = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            hashes = payload.get(primary_key)
            if hashes is None and fallback_key:
                hashes = payload.get(fallback_key, [])
            if isinstance(hashes, list):
                cleaned = [str(item) for item in hashes if str(item).strip()]
                return cleaned[-NEWS_HISTORY_LIMIT:]
        except Exception as exc:
            logger.warning("读取状态文件失败，将使用空状态: %s", exc)
        return []

    def _save_state(self) -> None:
        payload = {
            "started_at": self.started_at.strftime("%Y-%m-%d %H:%M:%S"),
            "pulled_fingerprints": self.pulled_keys[-NEWS_HISTORY_LIMIT:],
            "modeled_fingerprints": self.modeled_keys[-NEWS_HISTORY_LIMIT:],
            "sent_fingerprints": self.sent_keys[-NEWS_HISTORY_LIMIT:],
        }
        STATE_FILE.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def init_wechat(self, max_retries: int = 3) -> bool:
        for attempt in range(1, max_retries + 1):
            try:
                self.wx = WeChat()
                logger.info("微信实例初始化成功")
                return True
            except Exception as exc:
                logger.error("微信初始化失败(%s/%s): %s", attempt, max_retries, exc)
                if attempt < max_retries:
                    time.sleep(5)
        return False

    def ensure_target_chat(self) -> None:
        if not self.wx:
            raise RuntimeError("微信实例未初始化")

        target_candidates = [WECHAT_TARGET, *WECHAT_TARGET_ALIASES]
        tried: list[str] = []

        for target in target_candidates:
            for exact, force in ((True, False), (False, False), (False, True), (True, True)):
                tried.append(f"{target}|exact={exact}|force={force}")
                try:
                    activate_wechat_window()
                    self.wx.ChatWith(who=target, exact=exact, force=force, force_wait=0.8)
                    time.sleep(0.8)
                    current_chat = clean_text(self.wx.ChatInfo().get("chat_name"))
                    if normalize_name(current_chat) in {normalize_name(name) for name in target_candidates}:
                        return
                except Exception:
                    continue

        session_names = self.get_session_names()
        normalized_targets = {normalize_name(name) for name in target_candidates}
        for session_name in session_names:
            normalized_session = normalize_name(session_name)
            if any(target and target in normalized_session for target in normalized_targets):
                try:
                    activate_wechat_window()
                    self.wx.ChatWith(who=session_name, exact=True, force=True, force_wait=0.8)
                    time.sleep(0.8)
                    current_chat = clean_text(self.wx.ChatInfo().get("chat_name"))
                    if normalize_name(current_chat) == normalized_session:
                        logger.info("通过最近会话模糊匹配定位到目标群: %s", session_name)
                        return
                except Exception:
                    continue

        current_chat = clean_text(self.wx.ChatInfo().get("chat_name"))
        raise RuntimeError(
            "未能自动切换到目标群。"
            f"目标配置={target_candidates}，当前聊天窗口={current_chat or '未知'}，"
            f"最近会话候选={session_names[:15]}，尝试过程={tried}"
        )

    def get_session_names(self) -> list[str]:
        if not self.wx:
            return []
        try:
            sessions = self.wx.GetSession()
        except Exception as exc:
            logger.warning("读取最近会话失败: %s", exc)
            return []

        names: list[str] = []
        for session in sessions or []:
            for attr in ("name", "Name", "title"):
                value = getattr(session, attr, None)
                if value:
                    names.append(clean_text(value))
                    break
            else:
                text = clean_text(str(session))
                if text:
                    names.append(text)

        deduped: list[str] = []
        seen: set[str] = set()
        for name in names:
            if name and name not in seen:
                deduped.append(name)
                seen.add(name)
        return deduped

    def _acquire_send_lock(self):
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.CreateMutexW(None, False, WECHAT_SEND_MUTEX_NAME)
        if not handle:
            raise ctypes.WinError()
        wait_ms = max(1, WECHAT_SEND_LOCK_WAIT_SECONDS) * 1000
        logger.info("等待微信发送锁: %s", WECHAT_SEND_MUTEX_NAME)
        while True:
            result = kernel32.WaitForSingleObject(handle, wait_ms)
            if result in (0x00000000, 0x00000080):
                logger.info("已获取微信发送锁: %s", WECHAT_SEND_MUTEX_NAME)
                return handle
            if result == 0x00000102:
                logger.warning("微信发送锁等待超时，继续等待: %s", WECHAT_SEND_MUTEX_NAME)
                continue
            kernel32.CloseHandle(handle)
            raise ctypes.WinError()

    def _release_send_lock(self, handle) -> None:
        kernel32 = ctypes.windll.kernel32
        try:
            kernel32.ReleaseMutex(handle)
        finally:
            kernel32.CloseHandle(handle)
        logger.info("已释放微信发送锁: %s", WECHAT_SEND_MUTEX_NAME)

    def safe_send_msg(self, message: str, max_retries: int = 3) -> bool:
        if not self.wx:
            raise RuntimeError("微信实例未初始化")

        allowed_chats = {normalize_name(WECHAT_TARGET), *[normalize_name(x) for x in WECHAT_TARGET_ALIASES]}
        chunks = split_message(message, MAX_WECHAT_MESSAGE_LEN)
        lock_handle = self._acquire_send_lock()
        try:
            for chunk in chunks:
                sent = False
                for attempt in range(1, max_retries + 1):
                    try:
                        self.ensure_target_chat()
                        current_chat = clean_text(self.wx.ChatInfo().get("chat_name"))
                        if normalize_name(current_chat) not in allowed_chats:
                            raise RuntimeError(f"当前聊天窗口不是目标群: {current_chat}")
                        self.wx.SendMsg(msg=chunk)
                        sent = True
                        break
                    except Exception as exc:
                        logger.error("发送微信消息失败(%s/%s): %s", attempt, max_retries, exc)
                        if attempt < max_retries:
                            time.sleep(2)
                if not sent:
                    return False
                time.sleep(0.8)
            return True
        finally:
            self._release_send_lock(lock_handle)

    def fetch_news(self) -> list[NewsItem]:
        try:
            import akshare as ak
        except ImportError as exc:
            raise RuntimeError("未安装 akshare，请先执行 `pip install -r requirements.txt`") from exc

        fetchers = {
            "cls": lambda: fetch_cls_news(ak, AKSHARE_SYMBOL),
            "sina": lambda: fetch_sina_news(ak),
            "em": lambda: fetch_em_news(ak),
            "ths": lambda: fetch_ths_news(ak),
            "futu": lambda: fetch_futu_news(ak),
            "cx": lambda: fetch_cx_news(ak),
        }

        items: list[NewsItem] = []
        for source_name in AKSHARE_NEWS_SOURCES:
            fetcher = fetchers.get(source_name)
            if not fetcher:
                logger.warning("忽略未知的 AKShare 资讯源: %s", source_name)
                continue
            try:
                source_items = fetcher()
                logger.info("资讯源 %s 抓取到 %s 条", source_name, len(source_items))
                items.extend(source_items)
            except Exception as exc:
                logger.warning("资讯源 %s 抓取失败: %s", source_name, exc)

        items.sort(key=lambda item: item.published_at)
        return items

    def filter_recent_window_items(self, items: Iterable[NewsItem]) -> list[NewsItem]:
        window_items: list[NewsItem] = []
        cutoff_time = datetime.now() - timedelta(minutes=LOOKBACK_MINUTES)
        for item in items:
            if item.published_at < cutoff_time:
                continue
            window_items.append(item)
        return window_items

    def filter_unpulled_items(self, items: Iterable[NewsItem]) -> list[NewsItem]:
        result: list[NewsItem] = []
        for item in items:
            if item.dedupe_key in self.pulled_key_set:
                continue
            result.append(item)
        return result

    def filter_unmodeled_items(self, items: Iterable[NewsItem]) -> list[NewsItem]:
        result: list[NewsItem] = []
        for item in items:
            if item.dedupe_key in self.modeled_key_set:
                continue
            result.append(item)
        return result

    def summarize_batch(self, items: list[NewsItem]) -> str:
        if not items:
            return ""

        if not OPENAI_API_KEY:
            logger.warning("未配置 OPENAI_API_KEY，使用本地降级格式化")
            return render_fallback_message(items)

        system_prompt = (
            "你是市场资讯整理助手。"
            "请对输入的快讯做语义去重、合并相同事件、压缩措辞，但绝不编造事实。"
            "输出必须是简体中文纯文本，适合直接发微信群。"
            "输出格式必须严格如下：\n"
            "市场热点\n"
            "1. 句子；\n"
            "2. 句子；\n"
            "...\n\n"
            "要求：\n"
            "1. 不要输出时间、信息源、标题、前言、结语、Markdown。\n"
            "2. 每条只保留核心信息，写成一句话。\n"
            "3. 重复或高度相似内容要合并。\n"
            "4. 保持中文全角分号风格，最后一条可以不加分号。\n"
            "5. 条数由实际有效热点数量决定，不要强行凑成 3 条。\n"
            "6. 如果只有一条或两条，也保持同样结构。\n"
            "7. 只基于输入内容总结，不能增加未出现的信息。"
        )
        user_prompt = json.dumps(
            [item.to_prompt_dict() for item in items],
            ensure_ascii=False,
            indent=2,
        )
        try:
            response_text = call_openai_chat(system_prompt=system_prompt, user_prompt=user_prompt)
        except Exception as exc:
            logger.warning("大模型总结失败，回退到本地格式化发送: %s", exc)
            return render_fallback_message(items)
        return response_text.strip() if response_text.strip() else render_fallback_message(items)

    def _append_state_keys(self, items: Iterable[NewsItem], key_set: set[str], key_list: list[str]) -> None:
        for item in items:
            if item.dedupe_key in key_set:
                continue
            key_set.add(item.dedupe_key)
            key_list.append(item.dedupe_key)
        if len(key_list) > NEWS_HISTORY_LIMIT:
            del key_list[:-NEWS_HISTORY_LIMIT]
            key_set.clear()
            key_set.update(key_list)

    def mark_pulled(self, items: Iterable[NewsItem]) -> None:
        self._append_state_keys(items, self.pulled_key_set, self.pulled_keys)
        self._save_state()

    def mark_modeled(self, items: Iterable[NewsItem]) -> None:
        self._append_state_keys(items, self.modeled_key_set, self.modeled_keys)
        self._save_state()

    def mark_sent(self, items: Iterable[NewsItem]) -> None:
        self._append_state_keys(items, self.sent_key_set, self.sent_keys)
        self._save_state()

    def run_once(self) -> None:
        items = self.fetch_news()
        raw_count = len(items)
        window_items = self.filter_recent_window_items(items)
        window_count = len(window_items)
        pulled_new_items = self.filter_unpulled_items(window_items)
        self.mark_pulled(pulled_new_items)
        to_model_items = self.filter_unmodeled_items(pulled_new_items)
        deduped_for_model = dedupe_exact(to_model_items)

        logger.info(
            "本轮统计: 原始抓取数=%s / 窗口过滤后数=%s / 待模型数=%s / 去重后数=%s",
            raw_count,
            window_count,
            len(to_model_items),
            len(deduped_for_model),
        )

        if not deduped_for_model:
            logger.info("本轮最近 %s 分钟内无新快讯", LOOKBACK_MINUTES)
            return

        logger.info("本轮抓到 %s 条新快讯，开始总结并发送", len(deduped_for_model))
        message = self.summarize_batch(deduped_for_model)
        if not message:
            logger.warning("模型未返回可发送内容，本轮跳过")
            return

        self.mark_modeled(deduped_for_model)
        if self.safe_send_msg(message):
            self.mark_sent(deduped_for_model)
            logger.info("本轮发送完成")
        else:
            logger.error("本轮发送失败，未写入已发送状态")

    def run_forever(self) -> None:
        logger.info("机器人启动时间: %s", self.started_at.strftime("%Y-%m-%d %H:%M:%S"))
        logger.info("目标微信群: %s", WECHAT_TARGET)
        if WECHAT_TARGET_ALIASES:
            logger.info("目标群别名: %s", WECHAT_TARGET_ALIASES)
        logger.info("资讯源: %s", AKSHARE_NEWS_SOURCES)
        logger.info("新闻抓取时间窗口: 最近 %s 分钟", LOOKBACK_MINUTES)
        logger.info("轮询间隔: %s 秒", POLL_INTERVAL_SECONDS)
        logger.info("新闻去重历史容量: %s", NEWS_HISTORY_LIMIT)
        logger.info("OpenAI Base URL: %s", OPENAI_BASE_URL)
        logger.info("OpenAI Model: %s", OPENAI_MODEL)
        logger.info("OPENAI_API_KEY 已配置: %s", bool(OPENAI_API_KEY))
        logger.info("OpenAI 专用代理已配置: %s", bool(OPENAI_HTTP_PROXY or OPENAI_HTTPS_PROXY))

        while True:
            try:
                self.run_once()
            except Exception as exc:
                logger.exception("轮询执行异常: %s", exc)
            time.sleep(POLL_INTERVAL_SECONDS)


def clean_text(value: object) -> str:
    if value is None:
        return ""
    return " ".join(str(value).strip().split())


def normalize_name(value: str) -> str:
    return re.sub(r"\s+", "", clean_text(value)).lower()


def find_wechat_window() -> int | None:
    matches: list[int] = []

    def callback(hwnd: int, _: object) -> None:
        if not win32gui.IsWindowVisible(hwnd):
            return
        title = clean_text(win32gui.GetWindowText(hwnd))
        if title == "微信":
            matches.append(hwnd)

    win32gui.EnumWindows(callback, None)
    return matches[0] if matches else None


def activate_wechat_window() -> None:
    hwnd = find_wechat_window()
    if not hwnd:
        return

    try:
        if win32gui.IsIconic(hwnd):
            win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
        else:
            win32gui.ShowWindow(hwnd, win32con.SW_SHOW)
        win32gui.BringWindowToTop(hwnd)
        force_set_foreground_window(hwnd)
        time.sleep(0.3)
    except Exception as exc:
        logger.warning("激活微信窗口失败: %s", exc)


def force_set_foreground_window(hwnd: int) -> None:
    user32 = ctypes.windll.user32
    current_foreground = user32.GetForegroundWindow()
    current_thread = user32.GetWindowThreadProcessId(current_foreground, None)
    target_thread = user32.GetWindowThreadProcessId(hwnd, None)
    this_thread = win32api.GetCurrentThreadId()

    attached_threads: list[tuple[int, int]] = []
    for src, dst in ((this_thread, target_thread), (this_thread, current_thread), (current_thread, target_thread)):
        if src and dst and src != dst:
            user32.AttachThreadInput(src, dst, True)
            attached_threads.append((src, dst))

    try:
        user32.AllowSetForegroundWindow(-1)
        win32gui.SetForegroundWindow(hwnd)
        win32gui.SetActiveWindow(hwnd)
        win32gui.SetFocus(hwnd)
    finally:
        for src, dst in reversed(attached_threads):
            user32.AttachThreadInput(src, dst, False)


def parse_news_datetime(date_value: object, time_value: object | None = None) -> datetime | None:
    date_text = clean_text(date_value)
    time_text = clean_text(time_value) if time_value is not None else ""
    if not date_text and not time_text:
        return None

    raw = f"{date_text} {time_text}".strip()
    parsed: datetime | None = None
    try:
        parsed = date_parser.parse(raw, fuzzy=True)
    except Exception:
        if date_text:
            try:
                parsed = date_parser.parse(date_text, fuzzy=True)
            except Exception:
                parsed = None
        if parsed and time_text:
            try:
                parsed_time = date_parser.parse(time_text, fuzzy=True).time()
                parsed = datetime.combine(parsed.date(), parsed_time)
            except Exception:
                pass

    if not parsed:
        return None

    if parsed.year == 1900:
        now = datetime.now()
        parsed = parsed.replace(year=now.year, month=now.month, day=now.day)
        if parsed > now:
            parsed = parsed - timedelta(days=1)
    return parsed


def infer_source(title_value: object, content_value: object, default_source: str) -> str:
    text = " ".join(part for part in [clean_text(title_value), clean_text(content_value)] if part)
    if not text:
        return default_source

    match = re.search(r"[（(]([^()（）]{2,20})[）)]\s*$", text)
    if match:
        return match.group(1).strip()

    for source in ("新华社", "央视新闻", "界面新闻", "证券时报", "第一财经"):
        if source in text:
            return source
    return default_source


def dedupe_exact(items: list[NewsItem]) -> list[NewsItem]:
    seen: list[str] = []
    result: list[NewsItem] = []
    for item in items:
        normalized = canonicalize_news_text(item.title, item.content)
        if not normalized:
            continue
        duplicated = False
        for existing in seen:
            if is_near_duplicate(normalized, existing):
                duplicated = True
                break
        if duplicated:
            continue
        seen.append(normalized)
        result.append(item)
    return result


def canonicalize_news_text(title: str, content: str) -> str:
    text = clean_text(content or title or "")
    text = re.sub(r"^[【\[].*?[】\]]", "", text)
    text = re.sub(r"^(财联社|新浪财经|东方财富|同花顺|富途)[0-9月日:\- ]*电[，,：:]?", "", text)
    text = re.sub(r"\([^)]{2,20}\)$", "", text)
    text = re.sub(r"[（(][^()（）]{2,20}[）)]$", "", text)
    text = re.sub(r"\s+", "", text)
    text = re.sub(r"[，,。；;：:！!？?\-—_、\"'“”‘’\[\]【】()（）]", "", text)
    return text.strip().lower()


def is_near_duplicate(left: str, right: str) -> bool:
    if left == right:
        return True
    if left in right or right in left:
        shorter = min(len(left), len(right))
        longer = max(len(left), len(right))
        if shorter >= 12 and shorter / max(longer, 1) >= 0.7:
            return True
    left_bigrams = {left[i : i + 2] for i in range(max(len(left) - 1, 0))}
    right_bigrams = {right[i : i + 2] for i in range(max(len(right) - 1, 0))}
    if not left_bigrams or not right_bigrams:
        return False
    overlap = len(left_bigrams & right_bigrams) / max(min(len(left_bigrams), len(right_bigrams)), 1)
    return overlap >= 0.82


def render_fallback_message(items: list[NewsItem]) -> str:
    lines = ["市场热点"]
    for index, item in enumerate(items, start=1):
        content = clean_text(item.content or item.title)
        content = strip_trailing_punctuation(content)
        suffix = "；" if index < len(items) else ""
        lines.append(f"{index}.  {content}{suffix}")
    return "\n".join(lines).strip()


def strip_trailing_punctuation(text: str) -> str:
    return re.sub(r"[；;。.!！?？、，,]+$", "", text).strip()


def split_message(message: str, limit: int) -> list[str]:
    if len(message) <= limit:
        return [message]

    paragraphs = [part for part in message.split("\n\n") if part.strip()]
    chunks: list[str] = []
    current = ""
    for paragraph in paragraphs:
        candidate = paragraph if not current else f"{current}\n\n{paragraph}"
        if len(candidate) <= limit:
            current = candidate
            continue
        if current:
            chunks.append(current)
            current = ""
        if len(paragraph) <= limit:
            current = paragraph
            continue

        lines = paragraph.splitlines() or [paragraph]
        buffer = ""
        for line in lines:
            remaining = line
            while remaining:
                candidate_line = remaining if not buffer else f"{buffer}\n{remaining}"
                if len(candidate_line) <= limit:
                    buffer = candidate_line
                    remaining = ""
                else:
                    available = limit if not buffer else max(1, limit - len(buffer) - 1)
                    head = remaining[:available]
                    if buffer:
                        chunks.append(f"{buffer}\n{head}")
                        buffer = ""
                    else:
                        chunks.append(head)
                    remaining = remaining[available:]
        if buffer:
            current = buffer
    if current:
        chunks.append(current)
    return chunks


def build_openai_proxies() -> dict[str, str]:
    http_proxy = OPENAI_HTTP_PROXY or os.getenv("HTTP_PROXY", "").strip() or os.getenv("http_proxy", "").strip()
    https_proxy = OPENAI_HTTPS_PROXY or os.getenv("HTTPS_PROXY", "").strip() or os.getenv("https_proxy", "").strip()

    proxies: dict[str, str] = {}
    if http_proxy:
        proxies["http"] = http_proxy
    if https_proxy:
        proxies["https"] = https_proxy

    if OPENAI_NO_PROXY:
        os.environ["NO_PROXY"] = OPENAI_NO_PROXY
        os.environ["no_proxy"] = OPENAI_NO_PROXY
    return proxies


def call_openai_chat(system_prompt: str, user_prompt: str) -> str:
    url = f"{OPENAI_BASE_URL}/chat/completions"
    headers = {
        "Authorization": f"Bearer {OPENAI_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": OPENAI_MODEL,
        "temperature": 0.2,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
    }
    proxies = build_openai_proxies()
    last_error: Exception | None = None
    for attempt in range(1, OPENAI_MAX_RETRIES + 1):
        try:
            response = requests.post(
                url,
                headers=headers,
                json=payload,
                timeout=OPENAI_TIMEOUT_SECONDS,
                proxies=proxies or None,
            )
            response.raise_for_status()
            data = response.json()
            return data["choices"][0]["message"]["content"]
        except RequestException as exc:
            last_error = exc
            logger.warning("模型接口请求失败(%s/%s): %s", attempt, OPENAI_MAX_RETRIES, exc)
            if attempt < OPENAI_MAX_RETRIES:
                time.sleep(min(2 * attempt, 5))

    raise RuntimeError(
        f"调用模型接口失败，请检查 OPENAI_BASE_URL / 网络代理 / TLS 连通性。当前地址: {OPENAI_BASE_URL}；原始错误: {last_error}"
    ) from last_error


def fetch_cls_news(ak: object, symbol: str) -> list[NewsItem]:
    df = ak.stock_info_global_cls(symbol=symbol)
    if not isinstance(df, pd.DataFrame) or df.empty:
        return []
    items: list[NewsItem] = []
    for row in df.to_dict("records"):
        published_at = parse_news_datetime(row.get("发布日期"), row.get("发布时间"))
        if not published_at:
            continue
        title = clean_text(row.get("标题"))
        content = clean_text(row.get("内容"))
        if not title and not content:
            continue
        items.append(
            NewsItem(
                title=title,
                content=content,
                published_at=published_at,
                source=infer_source(title, content, "财联社"),
                channel="财联社",
            )
        )
    return items


def fetch_sina_news(ak: object) -> list[NewsItem]:
    df = ak.stock_info_global_sina()
    if not isinstance(df, pd.DataFrame) or df.empty:
        return []
    items: list[NewsItem] = []
    for row in df.to_dict("records"):
        published_at = parse_news_datetime(row.get("时间"))
        if not published_at:
            continue
        content = clean_text(row.get("内容"))
        if not content:
            continue
        items.append(
            NewsItem(
                title="",
                content=content,
                published_at=published_at,
                source=infer_source("", content, "新浪财经"),
                channel="新浪财经",
            )
        )
    return items


def fetch_em_news(ak: object) -> list[NewsItem]:
    df = ak.stock_info_global_em()
    if not isinstance(df, pd.DataFrame) or df.empty:
        return []
    items: list[NewsItem] = []
    for row in df.to_dict("records"):
        published_at = parse_news_datetime(row.get("发布时间"))
        if not published_at:
            continue
        title = clean_text(row.get("标题"))
        summary = clean_text(row.get("摘要"))
        if not title and not summary:
            continue
        items.append(
            NewsItem(
                title=title,
                content=summary or title,
                published_at=published_at,
                source="东方财富",
                channel="东方财富",
                link=clean_text(row.get("链接")),
            )
        )
    return items


def fetch_ths_news(ak: object) -> list[NewsItem]:
    df = ak.stock_info_global_ths()
    if not isinstance(df, pd.DataFrame) or df.empty:
        return []
    items: list[NewsItem] = []
    for row in df.to_dict("records"):
        published_at = parse_news_datetime(row.get("发布时间"))
        if not published_at:
            continue
        title = clean_text(row.get("标题"))
        content = clean_text(row.get("内容"))
        if not title and not content:
            continue
        items.append(
            NewsItem(
                title=title,
                content=content or title,
                published_at=published_at,
                source=infer_source(title, content, "同花顺"),
                channel="同花顺",
                link=clean_text(row.get("链接")),
            )
        )
    return items


def fetch_futu_news(ak: object) -> list[NewsItem]:
    df = ak.stock_info_global_futu()
    if not isinstance(df, pd.DataFrame) or df.empty:
        return []
    items: list[NewsItem] = []
    for row in df.to_dict("records"):
        published_at = parse_news_datetime(row.get("发布时间"))
        if not published_at:
            continue
        title = clean_text(row.get("标题"))
        content = clean_text(row.get("内容"))
        if not title and not content:
            continue
        items.append(
            NewsItem(
                title=title,
                content=content or title,
                published_at=published_at,
                source=infer_source(title, content, "富途"),
                channel="富途",
                link=clean_text(row.get("链接")),
            )
        )
    return items


def fetch_cx_news(ak: object) -> list[NewsItem]:
    df = ak.stock_news_main_cx()
    if not isinstance(df, pd.DataFrame) or df.empty:
        return []
    items: list[NewsItem] = []
    fetched_at = datetime.now()
    for row in df.to_dict("records"):
        tag = clean_text(row.get("tag"))
        summary = clean_text(row.get("summary"))
        if not summary:
            continue
        title = tag
        content = summary if not tag else f"{tag}：{summary}"
        items.append(
            NewsItem(
                title=title,
                content=content,
                published_at=fetched_at,
                source="财新精选",
                channel="财新精选",
                link=clean_text(row.get("url")),
            )
        )
    return items


def handle_exit(signum: int, frame: object) -> None:
    logger.info("收到退出信号 %s，程序结束", signum)
    raise SystemExit(0)


def main() -> None:
    signal.signal(signal.SIGINT, handle_exit)
    signal.signal(signal.SIGTERM, handle_exit)

    bot = MarketNewsBot()
    if not bot.init_wechat():
        raise SystemExit("微信初始化失败，请确认桌面微信已登录并可见")
    bot.run_forever()


if __name__ == "__main__":
    main()
