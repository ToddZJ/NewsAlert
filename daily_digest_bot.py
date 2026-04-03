from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import requests
from dotenv import load_dotenv
from requests import RequestException
from wxautox4 import WeChat


BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")
if not os.getenv("OPENAI_API_KEY"):
    load_dotenv(BASE_DIR / ".env.example", override=False)


LOG_FILE = os.getenv("DAILY_DIGEST_LOG_FILE", "daily_digest.log")
STATE_FILE = BASE_DIR / os.getenv("DAILY_DIGEST_STATE_FILE", "daily_digest_state.json")
WECHAT_TARGET = os.getenv("DAILY_DIGEST_TARGET", os.getenv("WECHAT_TARGET", "HAO"))
WECHAT_TARGET_ALIASES = [
    item.strip()
    for item in os.getenv("DAILY_DIGEST_TARGET_ALIASES", os.getenv("WECHAT_TARGET_ALIASES", "")).split(",")
    if item.strip()
]
DAILY_REPORT_TIME = os.getenv("DAILY_REPORT_TIME", "09:00")
LOOP_SLEEP_SECONDS = int(os.getenv("DAILY_DIGEST_LOOP_SECONDS", "30"))

GITHUB_TOP_N = int(os.getenv("GITHUB_TOP_N", "5"))
GITHUB_LOOKBACK_DAYS = int(os.getenv("GITHUB_LOOKBACK_DAYS", "1"))
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "").strip()
CLAWHUB_TOP_N = int(os.getenv("CLAWHUB_TOP_N", "5"))
CLAWHUB_QUERY_LIMIT = int(os.getenv("CLAWHUB_QUERY_LIMIT", "8"))
CLAWHUB_TOPIC_QUERIES = [
    item.strip()
    for item in os.getenv(
        "CLAWHUB_TOPIC_QUERIES",
        "agent,github,search,automation,news,wechat",
    ).split(",")
    if item.strip()
]

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
OPENAI_BASE_URL = os.getenv("OPENAI_BASE_URL", "https://api.siliconflow.cn/v1").rstrip("/")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "Qwen/Qwen2.5-7B-Instruct")
OPENAI_TIMEOUT_SECONDS = int(os.getenv("OPENAI_TIMEOUT_SECONDS", "90"))
OPENAI_MAX_RETRIES = int(os.getenv("OPENAI_MAX_RETRIES", "3"))
OPENAI_HTTP_PROXY = os.getenv("OPENAI_HTTP_PROXY", "").strip()
OPENAI_HTTPS_PROXY = os.getenv("OPENAI_HTTPS_PROXY", "").strip()
OPENAI_NO_PROXY = os.getenv("OPENAI_NO_PROXY", "").strip()
MAX_WECHAT_MESSAGE_LEN = int(os.getenv("MAX_WECHAT_MESSAGE_LEN", "1800"))


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] [%(levelname)s] [%(filename)s:%(lineno)d]  %(message)s",
    handlers=[
        logging.FileHandler(BASE_DIR / LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger(__name__)


@dataclass
class GitHubProject:
    full_name: str
    html_url: str
    description: str
    language: str
    stars: int
    created_at: str
    updated_at: str


@dataclass
class ClawHubProject:
    slug: str
    display_name: str
    summary: str
    updated_at: int
    score: float

    @property
    def url(self) -> str:
        return f"https://clawhub.ai/skills/{self.slug}"


class DailyDigestBot:
    def __init__(self) -> None:
        self.wx: WeChat | None = None
        self.state = self._load_state()

    def _load_state(self) -> dict[str, Any]:
        if not STATE_FILE.exists():
            return {}
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning("读取日报状态失败，将使用空状态: %s", exc)
            return {}

    def _save_state(self) -> None:
        STATE_FILE.write_text(
            json.dumps(self.state, ensure_ascii=False, indent=2),
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
        for target in target_candidates:
            for exact, force in ((True, False), (False, False), (False, True), (True, True)):
                try:
                    self.wx.ChatWith(who=target, exact=exact, force=force, force_wait=0.8)
                    time.sleep(0.8)
                    current_chat = clean_text(self.wx.ChatInfo().get("chat_name"))
                    if normalize_name(current_chat) in {normalize_name(name) for name in target_candidates}:
                        return
                except Exception:
                    continue

        raise RuntimeError(f"未能自动切换到目标群: {target_candidates}")

    def safe_send_msg(self, message: str, max_retries: int = 3) -> bool:
        if not self.wx:
            raise RuntimeError("微信实例未初始化")

        chunks = split_message(message, MAX_WECHAT_MESSAGE_LEN)
        allowed_chats = {normalize_name(WECHAT_TARGET), *[normalize_name(x) for x in WECHAT_TARGET_ALIASES]}

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
                    logger.error("发送日报失败(%s/%s): %s", attempt, max_retries, exc)
                    if attempt < max_retries:
                        time.sleep(2)
            if not sent:
                return False
            time.sleep(0.8)
        return True

    def should_send_today(self, now: datetime | None = None) -> bool:
        now = now or datetime.now()
        target_time = parse_daily_time(now, DAILY_REPORT_TIME)
        last_sent = clean_text(self.state.get("last_sent_date"))
        if last_sent == now.strftime("%Y-%m-%d"):
            return False
        return now >= target_time

    def fetch_github_projects(self) -> list[GitHubProject]:
        since = (datetime.now() - timedelta(days=GITHUB_LOOKBACK_DAYS)).strftime("%Y-%m-%d")
        headers = {"Accept": "application/vnd.github+json"}
        if GITHUB_TOKEN:
            headers["Authorization"] = f"Bearer {GITHUB_TOKEN}"

        response = requests.get(
            "https://api.github.com/search/repositories",
            params={
                "q": f"created:>={since} fork:false",
                "sort": "stars",
                "order": "desc",
                "per_page": GITHUB_TOP_N,
            },
            headers=headers,
            timeout=30,
        )
        response.raise_for_status()
        payload = response.json()
        items = payload.get("items", [])[:GITHUB_TOP_N]
        return [
            GitHubProject(
                full_name=item.get("full_name", ""),
                html_url=item.get("html_url", ""),
                description=clean_text(item.get("description")),
                language=clean_text(item.get("language")),
                stars=int(item.get("stargazers_count") or 0),
                created_at=clean_text(item.get("created_at")),
                updated_at=clean_text(item.get("updated_at")),
            )
            for item in items
        ]

    def fetch_clawhub_projects(self) -> list[ClawHubProject]:
        merged: dict[str, ClawHubProject] = {}
        for query in CLAWHUB_TOPIC_QUERIES:
            try:
                payload = clawhub_search(query=query)
            except Exception as exc:
                logger.warning("ClawHub 查询失败(%s): %s", query, exc)
                continue
            for item in payload.get("results", []):
                slug = clean_text(item.get("slug"))
                if not slug:
                    continue
                candidate = ClawHubProject(
                    slug=slug,
                    display_name=clean_text(item.get("displayName")) or slug,
                    summary=clean_text(item.get("summary")),
                    updated_at=int(item.get("updatedAt") or 0),
                    score=float(item.get("score") or 0),
                )
                current = merged.get(slug)
                if not current or (candidate.updated_at, candidate.score) > (current.updated_at, current.score):
                    merged[slug] = candidate
            if len(merged) >= max(CLAWHUB_TOP_N * 2, CLAWHUB_TOP_N):
                break

        ordered = sorted(
            merged.values(),
            key=lambda item: (item.updated_at, item.score),
            reverse=True,
        )
        return ordered[:CLAWHUB_TOP_N]

    def build_digest_message(self, github_projects: list[GitHubProject], clawhub_projects: list[ClawHubProject]) -> str:
        if OPENAI_API_KEY:
            try:
                return summarize_digest_with_llm(github_projects, clawhub_projects)
            except Exception as exc:
                logger.warning("大模型生成日报失败，回退到本地模板: %s", exc)
        return render_digest_fallback(github_projects, clawhub_projects)

    def send_digest(self) -> bool:
        github_projects = self.fetch_github_projects()
        clawhub_projects = self.fetch_clawhub_projects()
        message = self.build_digest_message(github_projects, clawhub_projects)
        if not message.strip():
            logger.warning("日报内容为空，跳过发送")
            return False
        if self.safe_send_msg(message):
            today = datetime.now().strftime("%Y-%m-%d")
            self.state["last_sent_date"] = today
            self.state["last_message_preview"] = message[:500]
            self._save_state()
            logger.info("日报发送完成: %s", today)
            return True
        return False

    def run_forever(self) -> None:
        logger.info("目标微信群: %s", WECHAT_TARGET)
        logger.info("日报发送时间: %s", DAILY_REPORT_TIME)
        logger.info("GitHub 项目数: %s", GITHUB_TOP_N)
        logger.info("ClawHub 项目数: %s", CLAWHUB_TOP_N)
        while True:
            try:
                if self.should_send_today():
                    self.send_digest()
            except Exception as exc:
                logger.exception("日报循环异常: %s", exc)
            time.sleep(LOOP_SLEEP_SECONDS)


def clean_text(value: object) -> str:
    if value is None:
        return ""
    return " ".join(str(value).strip().split())


def normalize_name(value: str) -> str:
    return "".join(clean_text(value).lower().split())


def split_message(message: str, limit: int) -> list[str]:
    if len(message) <= limit:
        return [message]
    lines = message.splitlines()
    chunks: list[str] = []
    current = ""
    for line in lines:
        candidate = line if not current else f"{current}\n{line}"
        if len(candidate) <= limit:
            current = candidate
        else:
            if current:
                chunks.append(current)
            current = line
    if current:
        chunks.append(current)
    return chunks


def parse_daily_time(now: datetime, hhmm: str) -> datetime:
    hour_str, minute_str = hhmm.split(":", 1)
    return now.replace(
        hour=int(hour_str),
        minute=int(minute_str),
        second=0,
        microsecond=0,
    )


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


def clawhub_search(query: str) -> dict[str, Any]:
    last_error: Exception | None = None
    for attempt in range(1, 4):
        try:
            response = requests.get(
                "https://clawhub.ai/api/v1/search",
                params={
                    "q": query,
                    "limit": CLAWHUB_QUERY_LIMIT,
                    "nonSuspiciousOnly": "true",
                },
                timeout=30,
            )
            if response.status_code == 429:
                retry_after = int(response.headers.get("Retry-After", "5"))
                logger.warning("ClawHub 搜索限流(%s)，%s 秒后重试", query, retry_after)
                time.sleep(retry_after)
                continue
            response.raise_for_status()
            return response.json()
        except RequestException as exc:
            last_error = exc
            if attempt < 3:
                time.sleep(attempt)
    raise RuntimeError(f"ClawHub 搜索失败: {query}; {last_error}") from last_error


def call_openai_chat(system_prompt: str, user_prompt: str) -> str:
    url = f"{OPENAI_BASE_URL}/chat/completions"
    headers = {
        "Authorization": f"Bearer {OPENAI_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": OPENAI_MODEL,
        "temperature": 0.3,
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
            logger.warning("日报模型请求失败(%s/%s): %s", attempt, OPENAI_MAX_RETRIES, exc)
            if attempt < OPENAI_MAX_RETRIES:
                time.sleep(min(2 * attempt, 5))
    raise RuntimeError(f"调用模型接口失败: {last_error}") from last_error


def summarize_digest_with_llm(
    github_projects: list[GitHubProject],
    clawhub_projects: list[ClawHubProject],
) -> str:
    system_prompt = (
        "你是技术情报日报编辑。"
        "请把输入的 GitHub 新热项目和 ClawHub 精选技能整理成一份中文日报，适合直接发微信群。"
        "目标是让读者快速跟进最新动态。"
        "输出必须是简体中文纯文本，结构严格如下：\n"
        "每日项目通报｜YYYY-MM-DD\n\n"
        "GitHub 新热项目\n"
        "1. 项目名：一句话亮点\n"
        "   - 为什么值得看\n"
        "   - 链接\n\n"
        "ClawHub 精选技能\n"
        "1. 技能名：一句话亮点\n"
        "   - 适合什么场景\n"
        "   - 链接\n\n"
        "今日建议\n"
        "- 给出 2 条简短行动建议\n\n"
        "要求：\n"
        "1. 不要编造数据，只基于输入整理。\n"
        "2. 语言紧凑，不要空话。\n"
        "3. 优先突出最新、最实用、最值得跟进的变化。"
    )
    payload = {
        "github": [project.__dict__ for project in github_projects],
        "clawhub": [project.__dict__ | {"url": project.url} for project in clawhub_projects],
        "date": datetime.now().strftime("%Y-%m-%d"),
    }
    return call_openai_chat(system_prompt=system_prompt, user_prompt=json.dumps(payload, ensure_ascii=False, indent=2)).strip()


def render_digest_fallback(
    github_projects: list[GitHubProject],
    clawhub_projects: list[ClawHubProject],
) -> str:
    today = datetime.now().strftime("%Y-%m-%d")
    lines = [f"每日项目通报｜{today}", "", "GitHub 新热项目"]
    if github_projects:
        for index, project in enumerate(github_projects, start=1):
            desc = project.description or "暂无描述"
            lang = f"｜{project.language}" if project.language else ""
            lines.extend(
                [
                    f"{index}. {project.full_name}：{desc}",
                    f"   - Star {project.stars}{lang}",
                    f"   - {project.html_url}",
                ]
            )
    else:
        lines.append("1. 今日未抓到 GitHub 新热项目")

    lines.extend(["", "ClawHub 精选技能"])
    if clawhub_projects:
        for index, project in enumerate(clawhub_projects, start=1):
            summary = project.summary or "暂无简介"
            lines.extend(
                [
                    f"{index}. {project.display_name}：{summary}",
                    f"   - slug: {project.slug}",
                    f"   - {project.url}",
                ]
            )
    else:
        lines.append("1. 今日未抓到 ClawHub 精选技能")

    lines.extend(
        [
            "",
            "今日建议",
            "- 先看 GitHub 前 2 个项目的 README，判断是否值得继续跟进。",
            "- 从 ClawHub 里挑 1 个技能实测，积累可复用工作流。",
        ]
    )
    return "\n".join(lines).strip()


def handle_exit(signum: int, frame: object) -> None:
    logger.info("收到退出信号 %s，程序结束", signum)
    raise SystemExit(0)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true", help="兼容参数：启动后只发送一次日报")
    parser.parse_args()

    signal.signal(signal.SIGINT, handle_exit)
    signal.signal(signal.SIGTERM, handle_exit)

    bot = DailyDigestBot()
    if not bot.init_wechat():
        raise SystemExit("微信初始化失败，请确认桌面微信已登录并可见")

    success = bot.send_digest()
    raise SystemExit(0 if success else 1)


if __name__ == "__main__":
    main()
