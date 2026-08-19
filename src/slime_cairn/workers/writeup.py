"""Asynchronous AI worker for the single, competition-wide CTF writeup.

This worker deliberately lives outside the solving scheduler.  A writeup is a
post-processing job over completed Blackboard records, so it must not create
Intents, submit flags, or consume a project solving lease.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import re
import threading
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen
from uuid import uuid4

from ..domain.models import Project
from ..server.blackboard import Blackboard
from ..server.writeups import (
    TEMPLATE_VERSION,
    collect_team_entries,
    generate_team_writeup,
    normalize_team_writeup,
    project_competition_key,
    template_text,
    validate_writeup,
)
from .model_gateway import validate_competition_model_route


_EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="writeup-worker")
_QUEUE_LOCK = threading.RLock()
_JOB_TTL_SECONDS = 30 * 60
_MAX_PROMPT_CHARS = 120_000
_MAX_FILE_CHARS = 50_000
_MAX_FILES_PER_PROJECT = 32
_MAX_WORKSPACE_CHARS = 14_000
_FORBIDDEN_RUNTIME_TERMS = re.compile(
    r"(?i)(?:\b(?:agent|worker|intent|blackboard|pseudopod|dispatcher)\b|智能体|工作线程|意图|黑板)"
)
_SCRIPT_SUFFIXES = frozenset(
    {
        ".asm",
        ".c",
        ".cc",
        ".cpp",
        ".go",
        ".h",
        ".hpp",
        ".java",
        ".js",
        ".lua",
        ".php",
        ".pl",
        ".py",
        ".rb",
        ".rs",
        ".s",
        ".sh",
        ".sql",
        ".ts",
        ".txt",
    }
)
_SKIP_PARTS = frozenset(
    {
        ".git",
        "node_modules",
        "__pycache__",
        ".slime-cairn-runtime",
        "pods",
        "transcripts",
        "sessions",
        "results",
    }
)


class WriteupWorkerError(RuntimeError):
    """A bounded writeup generation failure safe to expose in an audit event."""


def _env_first(*names: str) -> str:
    for name in names:
        value = str(os.environ.get(name, "")).strip()
        if value:
            return value
    return ""


def configured() -> bool:
    """Whether the dedicated worker has enough model settings to run."""

    return bool(
        _env_first("SLIME_WRITEUP_BASE_URL", "SLIME_CODEX_BASE_URL", "SLIME_LLM_BASE_URL")
        and _env_first("SLIME_WRITEUP_API_KEY", "SLIME_CODEX_API_KEY", "SLIME_LLM_API_KEY")
        and _env_first("SLIME_WRITEUP_MODEL", "SLIME_CODEX_MODEL", "SLIME_LLM_MODEL")
    )


def _redact(value: Any) -> str:
    """Keep secrets and credentials out of the model context and final WP."""

    text = str(value or "")
    text = re.sub(
        r"(?i)(authorization|access[_-]?key|api[_-]?key|token|password|secret|cookie)\s*[:=]\s*[^\s,;]+",
        lambda match: f"{match.group(1)}: [已脱敏]",
        text,
    )
    return text


def _workspace_root() -> Path:
    configured_root = _env_first("SLIME_WRITEUP_WORKSPACES_ROOT", "SLIME_WORKSPACES_ROOT")
    if configured_root:
        return Path(configured_root).expanduser().resolve()
    # This is useful for local development and tests.  Production compose
    # explicitly mounts the named workspace volume at /workspaces.
    return Path("/workspaces").resolve()


def _read_workspace_scripts(project_id: str) -> list[dict[str, str]]:
    root = (_workspace_root() / project_id).resolve()
    try:
        root.relative_to(_workspace_root())
    except ValueError:
        return []
    if not root.is_dir():
        return []
    found: list[dict[str, str]] = []
    seen_content: set[str] = set()
    total = 0
    try:
        paths = sorted(
            (path for path in root.rglob("*") if path.is_file()),
            key=lambda path: (
                0
                if re.search(r"(?i)(exploit|solve|payload|poc|exp|gen|flag)", path.name)
                else 1,
                str(path),
            ),
        )
    except OSError:
        return []
    for path in paths:
        if len(found) >= _MAX_FILES_PER_PROJECT or total >= _MAX_WORKSPACE_CHARS:
            break
        if any(part in _SKIP_PARTS for part in path.relative_to(root).parts):
            continue
        if path.suffix.lower() not in _SCRIPT_SUFFIXES:
            continue
        if path.name.lower() in {".env", "credentials", "secrets.json"}:
            continue
        try:
            raw = path.read_bytes()
        except OSError:
            continue
        if b"\x00" in raw[:4096]:
            continue
        remaining = _MAX_WORKSPACE_CHARS - total
        if remaining <= 0:
            break
        text = raw.decode("utf-8", errors="replace")[: min(_MAX_FILE_CHARS, remaining)]
        if not text.strip():
            continue
        relative = path.relative_to(root).as_posix()
        text = _redact(text)
        content_key = text.strip()
        if content_key in seen_content:
            continue
        seen_content.add(content_key)
        found.append({"path": relative, "content": text})
        total += len(text)
    return found


def _challenge_context(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    challenges: list[dict[str, Any]] = []
    for entry in entries:
        project: Project = entry["project"]
        metadata: dict[str, Any] = {}
        for key in ("agent_match", "benchmark"):
            value = project.scope.get(key)
            if isinstance(value, dict) and value.get("managed") is True:
                metadata = dict(value)
                break
        completion = entry.get("completion") or {}
        facts = [
            {
                "predicate": _redact(fact.predicate),
                "object": _redact(fact.object),
            }
            for fact in entry.get("facts", [])
            if str(fact.predicate).strip()
            not in {"project_origin", "project_goal", "agent_match_exercise_description"}
        ]
        challenges.append(
            {
                "name": _redact(metadata.get("name") or project.name),
                "category": _redact(metadata.get("category_name") or metadata.get("category") or "MISC"),
                "description": _redact(metadata.get("description") or project.goal),
                "difficulty": _redact(metadata.get("difficulty") or ""),
                "score": _redact(metadata.get("score") or metadata.get("total_score") or ""),
                "completion": _redact(completion.get("description")),
                "facts": facts,
                "scripts": _read_workspace_scripts(project.id),
            }
        )
    return challenges


def build_prompt(
    entries: list[dict[str, Any]],
    *,
    team: dict[str, Any] | None = None,
    existing_markdown: str = "",
) -> str:
    """Build a Chinese-only, evidence-bounded prompt for the model."""

    team = dict(team or {})
    context = {
        "team": {
            "name": _redact(team.get("name") or "待填写"),
            "rank": _redact(team.get("rank") or "待填写"),
            "model": _redact(team.get("model") or _env_first("SLIME_WRITEUP_MODEL", "SLIME_CODEX_MODEL")),
        },
        "challenges": _challenge_context(entries),
    }
    serialized = json.dumps(context, ensure_ascii=False, indent=2)
    if len(serialized) > _MAX_PROMPT_CHARS:
        serialized = serialized[:_MAX_PROMPT_CHARS] + "\n[后续超出长度的附件已省略]"
    old = _redact(existing_markdown)
    if len(old) > 25_000:
        old = old[:25_000] + "\n[旧稿后续已省略]"
    template = _redact(template_text())
    return f"""你是负责整理 CTF 比赛解题报告的中文技术作者。请根据下方已经确认的题目资料，直接输出一份完整 Markdown 团队 WP。

硬性要求：
1. 只输出 Markdown 正文，不要输出解释、前言、JSON 或代码围栏外的说明。
2. 全文使用中文；漏洞名、函数名、命令、协议、库名和代码中的标识符保留英文原文，并为英文技术事实补充中文解释。
3. 这是给参赛者和裁判阅读的做题报告，禁止写 Agent、Worker、Intent、Blackboard、Dispatcher、智能体、内部运行过程、提示词、模型调用、证据编号或调度步骤。
4. 不得编造资料中没有的 URL、端口、用户名、漏洞、脚本输出、Flag 或截图。缺少某项时，用“未记录”说明，不要凭空补全。
5. 按 Web、MISC、Crypto、REVERSE、PWN 分类；每道已完成题目都必须出现且只能根据对应资料书写。
6. 每道题必须包含：题目说明、漏洞/算法原理、完整的分步利用过程、关键命令或自编脚本、Flag 获取与平台验证、关键步骤截图占位。脚本应优先完整贴出；过长时贴出关键函数并解释参数。
7. Flag 只能照录资料中明确确认的最终答案，不能把示例、源码字符串或猜测当成 Flag。敏感凭据一律写“已脱敏”。
8. 必须完整保留下方模板的规则说明、团队信息字段、`### 二、解题过程` 和全部五个分类标题；只在相应分类下插入题目内容。不要删掉没有题目的分类，也不要输出模板示例题名或示例步骤。
   # 比赛解题报告
   ## 一、团队信息
   ### 二、解题过程
   每个分类使用三级标题，每道题使用四级标题，分类顺序固定为 Web、MISC、Crypto、REVERSE、PWN。
9. 截图占位请使用类似“![关键步骤截图](screenshots/题目名-关键步骤.png)”的 Markdown，不要省略该小节。

已确认题目资料（事实和脚本是唯一可信来源）：
{serialized}

比赛方 WP 模板（保留它要求的团队信息和分类结构；模板中的示例题名和占位文字不能进入最终稿）：
{template}

旧稿（仅用于保留已经人工补充的准确内容，不能覆盖新事实）：
{old or "无"}
"""


def _endpoint() -> tuple[str, str, str, str]:
    base = _env_first("SLIME_WRITEUP_BASE_URL", "SLIME_CODEX_BASE_URL", "SLIME_LLM_BASE_URL").rstrip("/")
    key = _env_first("SLIME_WRITEUP_API_KEY", "SLIME_CODEX_API_KEY", "SLIME_LLM_API_KEY")
    model = _env_first("SLIME_WRITEUP_MODEL", "SLIME_CODEX_MODEL", "SLIME_LLM_MODEL")
    protocol = _env_first("SLIME_WRITEUP_PROTOCOL") or "openai-responses"
    complete = _env_first("SLIME_WRITEUP_FULL_ENDPOINT", "SLIME_GATEWAY_FULL_ENDPOINT").lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    parsed = urlparse(base)
    if parsed.path.endswith("/chat/completions"):
        protocol = "openai-chat-completions"
        complete = True
    elif parsed.path.endswith("/messages"):
        protocol = "anthropic-messages"
        complete = True
    validate_competition_model_route("codex-cli", base, protocol, os.environ)
    if protocol == "anthropic-messages" and not complete:
        url = f"{base}/v1/messages"
    elif protocol == "openai-chat-completions" and not complete:
        url = f"{base}/chat/completions"
    elif protocol == "openai-responses" and not complete:
        url = f"{base}/responses"
    else:
        url = base
    return url, key, model, protocol


def _extract_text(payload: Any) -> str:
    if isinstance(payload, dict):
        direct = payload.get("output_text")
        if isinstance(direct, str) and direct.strip():
            return direct.strip()
        choices = payload.get("choices")
        if isinstance(choices, list) and choices:
            message = choices[0].get("message") if isinstance(choices[0], dict) else None
            if isinstance(message, dict):
                content = message.get("content")
                if isinstance(content, str) and content.strip():
                    return content.strip()
                if isinstance(content, list):
                    texts = [item.get("text", "") for item in content if isinstance(item, dict)]
                    if any(str(item).strip() for item in texts):
                        return "\n".join(str(item) for item in texts).strip()
        output = payload.get("output")
        if isinstance(output, list):
            chunks: list[str] = []
            for item in output:
                if not isinstance(item, dict):
                    continue
                item_type = str(item.get("type") or "").strip().lower()
                if item_type in {"analysis", "reasoning", "reasoning_text", "thinking"}:
                    continue
                if item_type and item_type not in {"message", "output_text", "text"} and item.get("role") != "assistant":
                    continue
                content = item.get("content")
                if isinstance(content, str):
                    chunks.append(content)
                elif isinstance(content, list):
                    chunks.extend(
                        str(part.get("text", ""))
                        for part in content
                        if isinstance(part, dict)
                        and str(part.get("type") or "").strip().lower()
                        not in {"analysis", "reasoning", "reasoning_text", "thinking"}
                        and str(part.get("text", "")).strip()
                    )
            if chunks:
                return "\n".join(chunks).strip()
    raise WriteupWorkerError("模型没有返回可解析的 Markdown")


def call_model(prompt: str) -> tuple[str, dict[str, Any]]:
    url, api_key, model, protocol = _endpoint()
    if not url or not api_key or not model:
        raise WriteupWorkerError("Writeup Worker 的模型网关配置不完整")
    if protocol == "anthropic-messages":
        body: dict[str, Any] = {
            "model": model,
            "max_tokens": int(_env_first("SLIME_WRITEUP_MAX_OUTPUT_TOKENS") or "16000"),
            "messages": [{"role": "user", "content": prompt}],
        }
        headers = {"anthropic-version": "2023-06-01"}
    elif protocol == "openai-chat-completions":
        body = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": int(_env_first("SLIME_WRITEUP_MAX_OUTPUT_TOKENS") or "16000"),
        }
        headers = {}
    else:
        body = {
            "model": model,
            "input": [{"role": "user", "content": prompt}],
            "max_output_tokens": int(_env_first("SLIME_WRITEUP_MAX_OUTPUT_TOKENS") or "32000"),
            "reasoning": {"effort": _env_first("SLIME_WRITEUP_REASONING_EFFORT") or "low"},
        }
        headers = {}
    headers.update(
        {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "User-Agent": "slime-cairn-writeup-worker/1",
        }
    )
    request = Request(url, data=json.dumps(body, ensure_ascii=False).encode("utf-8"), headers=headers, method="POST")
    timeout = float(_env_first("SLIME_WRITEUP_TIMEOUT") or "300")
    retries = max(0, min(5, int(_env_first("SLIME_WRITEUP_RETRIES") or "3")))
    retry_base = max(1.0, float(_env_first("SLIME_WRITEUP_RETRY_SECONDS") or "10"))
    raw = ""
    status = 0
    for attempt in range(retries + 1):
        try:
            with urlopen(request, timeout=timeout) as response:
                raw = response.read(2_000_000).decode("utf-8", errors="replace")
                status = int(response.getcode())
            break
        except HTTPError as exc:
            detail = ""
            try:
                detail = exc.read(4000).decode("utf-8", errors="replace")
            except OSError:
                pass
            compact = " ".join(detail.split())[:500]
            retryable = exc.code in {408, 429, 500, 502, 503, 504, 520, 521, 522, 523, 524} or any(
                marker in compact.lower()
                for marker in ("rate increased", "rate limit", "too many requests", "temporarily unavailable")
            )
            if retryable and attempt < retries:
                time.sleep(retry_base * (2**attempt))
                continue
            raise WriteupWorkerError(f"模型网关 HTTP {exc.code}: {compact}") from exc
        except (URLError, OSError, TimeoutError) as exc:
            if attempt < retries:
                time.sleep(retry_base * (2**attempt))
                continue
            raise WriteupWorkerError(f"模型网关请求失败: {type(exc).__name__}") from exc
    if status < 200 or status >= 300:
        raise WriteupWorkerError(f"模型网关返回 HTTP {status}")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise WriteupWorkerError("模型网关返回不是 JSON") from exc
    return _extract_text(payload), {"model": model, "endpoint": url, "status": status}


def _validate_ai_markdown(markdown: str, entries: list[dict[str, Any]]) -> dict[str, Any]:
    report = validate_writeup(markdown)
    report["worker"] = "writeup-worker"
    if _FORBIDDEN_RUNTIME_TERMS.search(markdown):
        report["ready"] = False
        report["status"] = "incomplete"
        report["forbidden_runtime_terms"] = True
    else:
        report["forbidden_runtime_terms"] = False
    if not markdown.lstrip().startswith("# "):
        report["ready"] = False
        report["status"] = "incomplete"
    chinese_chars = len(re.findall(r"[\u4e00-\u9fff]", markdown))
    minimum_chinese_chars = max(120, len(entries) * 180)
    report["chinese_chars"] = chinese_chars
    if chinese_chars < minimum_chinese_chars:
        report["ready"] = False
        report["status"] = "incomplete"
        report["insufficient_chinese_content"] = True
    else:
        report["insufficient_chinese_content"] = False
    missing_challenges = []
    for entry in entries:
        project: Project = entry["project"]
        metadata = next(
            (
                value
                for key in ("agent_match", "benchmark")
                for value in [project.scope.get(key)]
                if isinstance(value, dict) and value.get("managed") is True
            ),
            {},
        )
        name = str(metadata.get("name") or project.name).strip()
        if name and name not in markdown:
            missing_challenges.append(name)
    report["missing_challenges"] = missing_challenges
    if missing_challenges:
        report["ready"] = False
        report["status"] = "incomplete"
    return report


def _job_running(row: dict[str, Any] | None) -> bool:
    if not row:
        return False
    validation = row.get("validation") or {}
    state = str(validation.get("worker_status") or "")
    started = float(validation.get("worker_started_at") or 0)
    return state in {"pending", "running"} and (time.time() - started) < _JOB_TTL_SECONDS


def queue_team_writeup(
    board: Blackboard,
    project_id: str,
    *,
    team: dict[str, Any] | None = None,
    force: bool = False,
) -> dict[str, Any] | None:
    """Queue one team WP and return its current persisted record immediately."""

    project = board.get_project(project_id)
    task_key = project_competition_key(project)
    if not task_key:
        return None
    existing = board.get_competition_writeup(task_key)
    if not force and _job_running(existing):
        return existing
    entries = collect_team_entries(board, task_key)
    baseline, baseline_report = generate_team_writeup(entries, team=team)
    old_markdown = str(existing.get("markdown") or "") if existing else ""
    prompt_markdown = old_markdown if existing and existing.get("status") == "ready" else ""
    # Preserve an existing human/AI draft while a retry is pending.  A fresh
    # competition with no draft receives the deterministic evidence-backed
    # skeleton as a useful fallback if the model is unavailable.
    markdown = old_markdown or baseline
    job_id = f"writeup-job-{uuid4().hex}"
    now = time.time()
    pending_report = dict((existing or {}).get("validation") or {})
    pending_report.update(
        {
            "worker": "writeup-worker",
            "worker_status": "pending",
            "job_id": job_id,
            "worker_started_at": now,
            "challenge_count": len(entries),
            "template_version": TEMPLATE_VERSION,
        }
    )
    with _QUEUE_LOCK:
        row = board.save_competition_writeup(
            task_key,
            markdown,
            "pending",
            TEMPLATE_VERSION,
            pending_report,
        )
        board.add_event(project_id, "writeup.worker_queued", {"task_key": task_key, "job_id": job_id})
        _EXECUTOR.submit(
            _run_job,
            board,
            project_id,
            task_key,
            job_id,
            entries,
            team or {},
            markdown,
            prompt_markdown,
            baseline_report,
        )
    return row


def _run_job(
    board: Blackboard,
    project_id: str,
    task_key: str,
    job_id: str,
    entries: list[dict[str, Any]],
    team: dict[str, Any],
    old_markdown: str,
    prompt_markdown: str,
    baseline_report: dict[str, Any],
) -> None:
    current = board.get_competition_writeup(task_key)
    validation = dict((current or {}).get("validation") or {})
    validation.update({"worker": "writeup-worker", "worker_status": "running", "job_id": job_id})
    board.save_competition_writeup(task_key, old_markdown, "running", TEMPLATE_VERSION, validation)
    board.add_event(project_id, "writeup.worker_started", {"task_key": task_key, "job_id": job_id})
    try:
        prompt = build_prompt(entries, team=team, existing_markdown=prompt_markdown)
        markdown, model_info = call_model(prompt)
        markdown = markdown.strip()
        if markdown.startswith("```markdown") and markdown.endswith("```"):
            markdown = markdown[len("```markdown") : -3].strip()
        markdown = normalize_team_writeup(markdown, entries, team=team)
        report = _validate_ai_markdown(markdown, entries)
        report.update(
            {
                "worker_status": "ready" if report.get("ready") else "incomplete",
                "job_id": job_id,
                "challenge_count": len(entries),
                "template_version": TEMPLATE_VERSION,
                "model": model_info.get("model", ""),
            }
        )
        latest = board.get_competition_writeup(task_key)
        if str((latest or {}).get("validation", {}).get("job_id") or "") != job_id:
            return
        board.save_competition_writeup(
            task_key,
            markdown,
            report["status"],
            TEMPLATE_VERSION,
            report,
        )
        board.save_project_writeup(project_id, markdown, report["status"], TEMPLATE_VERSION, report)
        board.add_event(project_id, "writeup.worker_finished", {"task_key": task_key, "job_id": job_id, "status": report["status"]})
    except Exception as exc:
        latest = board.get_competition_writeup(task_key)
        if str((latest or {}).get("validation", {}).get("job_id") or "") != job_id:
            return
        failed = dict(baseline_report)
        failed.update(
            {
                "worker": "writeup-worker",
                "worker_status": "failed",
                "job_id": job_id,
                "challenge_count": len(entries),
                "error": f"{type(exc).__name__}: {exc}"[:1000],
            }
        )
        # Keep the old markdown exactly as it was when this job was queued.
        board.save_competition_writeup(task_key, old_markdown, "failed", TEMPLATE_VERSION, failed)
        board.add_event(project_id, "writeup.worker_failed", {"task_key": task_key, "job_id": job_id, "error": failed["error"]})


__all__ = ["build_prompt", "call_model", "configured", "queue_team_writeup"]
