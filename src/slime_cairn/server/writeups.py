"""Evidence-backed Writeup generation and validation."""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
from typing import Any

from ..domain.models import Fact, Project


TEMPLATE_VERSION = "2"
DEFAULT_TEMPLATE = Path(__file__).parents[1] / "writeup_template.md"
SENSITIVE_RE = re.compile(
    r"(?i)(authorization|access[_-]?key|api[_-]?key|token|password|secret|cookie)\s*[:=]\s*[^\s,;]+"
)
SECTIONS = {
    "web": "Web",
    "misc": "MISC",
    "crypto": "Crypto",
    "reverse": "REVERSE",
    "re": "REVERSE",
    "pwn": "PWN",
}
CATEGORY_ORDER = ("Web", "MISC", "Crypto", "REVERSE", "PWN")


def _normal_category(value: Any) -> str:
    raw = str(value or "").strip().casefold()
    compact = re.sub(r"[\s_\-]+", "", raw)
    aliases = {
        "web": "Web",
        "misc": "MISC",
        "miscellaneous": "MISC",
        "crypto": "Crypto",
        "cryptography": "Crypto",
        "reverse": "REVERSE",
        "re": "REVERSE",
        "reverseengineering": "REVERSE",
        "pwn": "PWN",
        "binary": "PWN",
        "binaryexploitation": "PWN",
    }
    return aliases.get(compact, "")


def project_competition_key(project: Project) -> str | None:
    """Return the team-wide Writeup key for a managed project."""

    for platform in ("agent_match", "benchmark"):
        metadata = project.scope.get(platform)
        if isinstance(metadata, dict) and metadata.get("managed") is True:
            task_key = str(metadata.get("task_key") or "").strip()
            if task_key:
                return f"{platform}:{task_key}"
    return None


def collect_team_entries(board: Any, task_key: str) -> list[dict[str, Any]]:
    """Collect completed projects belonging to one competition task."""

    entries: list[dict[str, Any]] = []
    for project in board.list_projects(include_deleting=True):
        if project.status != "completed" or project_competition_key(project) != task_key:
            continue
        entries.append(
            {
                "project": project,
                "completion": board.get_active_completion(project.id),
                "facts": board.list_facts(project.id),
                "evidence": board.list_evidence(project.id),
                "worker_runs": board.list_worker_runs(project.id),
            }
        )
    return entries


def template_text() -> str:
    configured = str(os.environ.get("SLIME_WRITEUP_TEMPLATE_PATH", "")).strip()
    path = Path(configured).expanduser() if configured else DEFAULT_TEMPLATE
    try:
        return path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeError):
        return DEFAULT_TEMPLATE.read_text(encoding="utf-8")


def _text(value: Any, fallback: str = "待填写") -> str:
    text = str(value or "").strip()
    return text or fallback


def _metadata(project: Project) -> dict[str, Any]:
    for key in ("agent_match", "benchmark"):
        value = project.scope.get(key)
        if isinstance(value, dict) and value.get("managed") is True:
            return value
    return {}


def _category(metadata: dict[str, Any]) -> str:
    for key in ("category_name", "category", "type", "challenge_type"):
        normalized = _normal_category(metadata.get(key))
        if normalized:
            return normalized
    return "MISC"


def _safe(value: Any, fallback: str = "待填写") -> str:
    if isinstance(value, (dict, list)):
        value = json.dumps(value, ensure_ascii=False, sort_keys=True)
    text = str(value or "").strip() or fallback
    return SENSITIVE_RE.sub(lambda match: f"{match.group(1)}: [已脱敏]", text)


def _fact_lines(facts: list[Fact], predicates: set[str] | None = None) -> list[str]:
    selected = facts if predicates is None else [fact for fact in facts if fact.predicate in predicates]
    lines: list[str] = []
    for fact in selected:
        evidence = ", ".join(fact.evidence_refs) or "无登记证据"
        lines.append(f"- **{_safe(fact.predicate)}**：{_safe(fact.object)}（证据：{_safe(evidence)}）")
    return lines


def _clean_technical_text(value: Any) -> str:
    """Remove runtime narration while preserving challenge-solving details."""

    text = _safe(value, "").replace("\r", " ").replace("\n", " ")
    text = re.sub(r"https?://\S+", "[附件地址]", text)
    text = re.sub(r"(?:/workspace|/tmp|pods/)[^\s,;，。)]*", "[本地分析文件]", text)
    text = re.sub(r"(?i)Evidence(?:/work)?(?: files)?\s*:[^.。]*[.。]?", "", text)
    replacements = (
        (r"(?i)confirmed objective results", "已确认的技术结果"),
        (r"(?i)confirmed (?:this session|this pass)", "已确认"),
        (r"(?i)explored (?:competition )?exercise[^.:：]*[:：]?", "题目分析："),
        (r"(?i)static reverse engineering", "静态逆向分析"),
        (r"(?i)the vulnerability path", "漏洞利用路径"),
        (r"(?i)use-after-free", "释放后使用（UAF）"),
        (r"(?i)double-free", "双重释放"),
        (r"(?i)tcache poisoning", "tcache 投毒"),
        (r"(?i)unsorted-bin libc leak", "通过 unsorted bin 泄漏 libc 地址"),
        (r"(?i)no (?:final )?flag value was (?:retrieved|obtained|confirmed)[^.。]*[.。]?", ""),
        (r"(?i)no answer candidate[^.。]*[.。]?", ""),
        (r"(?i)(?:this|the) (?:session|pass) (?:was )?(?:cut short|timed out)[^.。]*[.。]?", ""),
        (r"(?i)conclude(?:-phase| phase)?[^.:：]*[:：]?", ""),
    )
    for pattern, replacement in replacements:
        text = re.sub(pattern, replacement, text)
    return " ".join(text.split()).strip(" ;；")


def _solution_lines(facts: list[Fact]) -> list[str]:
    lines: list[str] = []
    for fact in facts:
        predicate = fact.predicate
        if predicate in {
            "project_origin",
            "project_goal",
            "agent_match_exercise_description",
            "agent_match_flag_rejected",
            "benchmark_flag_rejected",
        }:
            continue
        if predicate.endswith("_completion_verified"):
            lines.append("- 平台已确认该题完成，最终结果校验通过。")
            continue
        if predicate.endswith("_flag_verified"):
            lines.append(f"- 平台验证通过的答案：`{_safe(fact.object)}`")
            continue
        cleaned = _clean_technical_text(fact.object)
        if cleaned:
            lines.append(f"- {cleaned}")
    return list(dict.fromkeys(lines))


def _run_model(run: dict[str, Any]) -> str:
    session = run.get("model_session") or {}
    direct = session.get("model")
    if direct:
        return str(direct).strip()
    health = session.get("health") or {}
    if isinstance(health, dict) and isinstance(health.get("model"), dict):
        return str(health["model"].get("model") or "").strip()
    manifest = run.get("context_manifest") or {}
    return str(manifest.get("model") or "").strip()


def _render_template(
    *,
    title: str,
    team_name: str,
    rank: str,
    solved_count: int | str,
    token_total: int,
    model_name: str,
    sections: dict[str, list[str]],
) -> str:
    """Render the configured WP template as an immutable document skeleton."""

    template = template_text().replace("\r\n", "\n").replace("\r", "\n")
    first_category = re.search(
        rf"(?m)^###\s+(?:{'|'.join(re.escape(item) for item in CATEGORY_ORDER)})\s*$",
        template,
    )
    if first_category is None:
        template = DEFAULT_TEMPLATE.read_text(encoding="utf-8").replace("\r\n", "\n").replace("\r", "\n")
        first_category = re.search(r"(?m)^###\s+Web\s*$", template)
    prefix = template[: first_category.start()].rstrip() if first_category else template.rstrip()
    prefix = re.sub(r"(?m)^#\s+.*$", lambda _: f"# {_safe(title)}", prefix, count=1)
    fields = {
        "名称": team_name,
        "排名": rank,
        "解题数量": str(solved_count),
        "消耗token数": str(token_total),
        "模型名称": model_name,
    }
    for label, value in fields.items():
        prefix = re.sub(
            rf"(?m)^-\s*{re.escape(label)}\s*[：:].*$",
            lambda _, label=label, value=value: f"- {label}：{_safe(value)}",
            prefix,
            count=1,
        )
    parts = [prefix]
    for category in CATEGORY_ORDER:
        parts.extend(["", f"### {category}"])
        for body in sections.get(category, []):
            if body.strip():
                parts.extend(["", body.strip()])
    return "\n".join(parts).strip() + "\n"


def _challenge_markdown(
    project: Project,
    completion: dict[str, Any] | None,
    facts: list[Fact],
) -> str:
    """Render one evidence-backed challenge body without a category heading."""

    metadata = _metadata(project)
    name = _text(metadata.get("name") or project.name, project.name)
    score = metadata.get("score", metadata.get("total_score", "待填写"))
    endpoint_values = (
        metadata.get("endpoints")
        or metadata.get("container_addr")
        or project.scope.get("targets")
        or [project.target]
    )
    if isinstance(endpoint_values, str):
        endpoint_values = [endpoint_values]
    endpoints = ", ".join(_safe(item) for item in endpoint_values if str(item).strip())
    lines = [
        f"#### {_safe(name)}",
        "",
        f"- 题目描述：{_safe(metadata.get('description') or project.goal)}",
        f"- 难度：{_safe(metadata.get('difficulty'))}",
        f"- 分值：{_safe(score)}",
        f"- 题目环境：{endpoints or '待填写'}",
        "",
        "##### 关键原理与漏洞分析",
        "",
    ]
    solution_lines = _solution_lines(facts)
    lines.extend(solution_lines or ["- 待补充：当前记录中没有足够的题目技术分析。"])
    lines.extend(
        [
            "",
            "##### 解题步骤",
            "",
            "1. 根据题目描述确认考查方向，并准备对应的附件或运行环境。",
            "2. 按上方漏洞/算法分析复现关键条件，构造利用链或求解过程。",
            "3. 运行脚本或完成手工操作，取得题目要求的最终答案。",
            "4. 将答案提交平台验证，确认题目状态变为已完成。",
            "",
            f"- 最终结论：{_safe((completion or {}).get('description'), '平台已验证完成；详细结论待补充。')}",
            "",
            "##### 自编脚本",
            "",
            "待补充：请贴出自行编写脚本的完整内容或关键函数。",
            "",
            "##### 关键步骤截图",
            "",
            "待补充：请附关键步骤截图（建议使用证据引用或相对路径）。",
        ]
    )
    return "\n".join(lines).strip()


def _team_metrics(entries: list[dict[str, Any]], team: dict[str, Any]) -> tuple[int, list[str], str]:
    total_tokens = sum(
        int((run.get("model_usage") or {}).get("total_tokens") or 0)
        for entry in entries
        for run in entry.get("worker_runs", [])
    )
    models = sorted(
        {
            _run_model(run)
            for entry in entries
            for run in entry.get("worker_runs", [])
            if _run_model(run)
        }
    )
    model_name = _safe(team.get("model") or ", ".join(models), "待填写")
    return total_tokens, models, model_name


def generate_writeup(
    project: Project,
    completion: dict[str, Any] | None,
    facts: list[Fact],
    evidence: list[dict[str, Any]],
    worker_runs: list[dict[str, Any]],
    *,
    team: dict[str, Any] | None = None,
    existing_markdown: str = "",
) -> tuple[str, dict[str, Any]]:
    """Generate a deterministic draft and return its validation report."""

    team = dict(team or {})
    total_tokens = sum(int((run.get("model_usage") or {}).get("total_tokens") or 0) for run in worker_runs)
    models = sorted(
        {
            _run_model(run)
            for run in worker_runs
            if _run_model(run)
        }
    )
    metadata = _metadata(project)
    name = _text(metadata.get("name") or project.name, project.name)
    category = _category(metadata)
    team_name = _safe(team.get("name"), "待填写")
    rank = _safe(team.get("rank") or metadata.get("rank"), "待填写")
    solved_count = team.get("solved_count", "待填写")
    model_name = _safe(team.get("model") or ", ".join(models), "待填写")
    markdown = _render_template(
        title=name,
        team_name=team_name,
        rank=rank,
        solved_count=solved_count,
        token_total=total_tokens,
        model_name=model_name,
        sections={category: [_challenge_markdown(project, completion, facts)]},
    )
    # Preserve human-authored sections when explicitly supplied through PUT.
    if existing_markdown.strip():
        markdown = existing_markdown.strip() + "\n"
    report = validate_writeup(markdown)
    report.update({"template_version": TEMPLATE_VERSION, "token_total": total_tokens, "model_names": models})
    return markdown, report


def validate_writeup(markdown: str) -> dict[str, Any]:
    text = str(markdown or "")
    required = [
        "# ",
        "## 一、团队信息",
        "### 二、解题过程",
        "自编脚本",
        "关键步骤截图",
        *[f"### {category}" for category in CATEGORY_ORDER],
    ]
    missing = [marker for marker in required if marker not in text]
    category_positions = [text.find(f"### {category}") for category in CATEGORY_ORDER]
    if any(position < 0 for position in category_positions) or category_positions != sorted(category_positions):
        missing.append("分类顺序：Web / MISC / Crypto / REVERSE / PWN")
    placeholders = text.count("待补充") + text.count("待填写")
    sensitive = bool(SENSITIVE_RE.search(text))
    ready = not missing and not sensitive and placeholders == 0
    return {
        "status": "ready" if ready else "incomplete",
        "ready": ready,
        "missing_sections": missing,
        "placeholder_count": placeholders,
        "sensitive_content": sensitive,
    }


def generate_team_writeup(
    entries: list[dict[str, Any]],
    *,
    team: dict[str, Any] | None = None,
) -> tuple[str, dict[str, Any]]:
    """Combine every completed challenge in one submission-ready Markdown file."""

    team = dict(team or {})
    total_tokens, models, model_name = _team_metrics(entries, team)
    team_name = _safe(team.get("name"), "待填写")
    rank = _safe(team.get("rank"), "待填写")
    title = _safe(team.get("title"), "比赛解题报告")
    grouped: dict[str, list[str]] = {category: [] for category in CATEGORY_ORDER}
    for entry in entries:
        category = _category(_metadata(entry["project"]))
        grouped[category].append(
            _challenge_markdown(entry["project"], entry.get("completion"), entry.get("facts", []))
        )
    markdown = _render_template(
        title=title,
        team_name=team_name,
        rank=rank,
        solved_count=len(entries),
        token_total=total_tokens,
        model_name=model_name,
        sections=grouped,
    )
    validation = validate_writeup(markdown)
    validation.update(
        {
            "template_version": TEMPLATE_VERSION,
            "token_total": total_tokens,
            "model_names": models,
            "challenge_count": len(entries),
        }
    )
    return markdown, validation


def _ai_challenge_blocks(markdown: str) -> dict[str, list[tuple[str, str]]]:
    """Extract level-four challenge sections while ignoring model metadata."""

    blocks: dict[str, list[tuple[str, str]]] = {category: [] for category in CATEGORY_ORDER}
    heading_re = re.compile(r"(?m)^(#{3,4})[ \t]+(.+?)[ \t]*$")
    matches = list(heading_re.finditer(str(markdown or "")))
    current_category: str | None = None
    for index, match in enumerate(matches):
        level = len(match.group(1))
        heading = match.group(2).strip()
        if level == 3:
            current_category = _normal_category(heading) or None
            continue
        if current_category is None:
            continue
        end = matches[index + 1].start() if index + 1 < len(matches) else len(markdown)
        body = str(markdown)[match.start() : end].strip()
        blocks[current_category].append((heading, body))
    return blocks


def _ensure_challenge_sections(body: str) -> str:
    """Keep required per-challenge headings even when the model omits them."""

    text = body.strip()
    additions: list[str] = []
    if "自编脚本" not in text:
        additions.extend(["##### 自编脚本", "", "未记录"])
    if "关键步骤截图" not in text:
        additions.extend(["##### 关键步骤截图", "", "未记录"])
    if additions:
        text = f"{text}\n\n" + "\n".join(additions)
    return text


def normalize_team_writeup(
    markdown: str,
    entries: list[dict[str, Any]],
    *,
    team: dict[str, Any] | None = None,
) -> str:
    """Keep model-authored challenge prose but force the configured template shape."""

    team = dict(team or {})
    for key, label in (("name", "名称"), ("rank", "排名"), ("model", "模型名称")):
        if str(team.get(key) or "").strip():
            continue
        match = re.search(
            rf"(?m)^-[ \t]*{re.escape(label)}[ \t]*[：:][ \t]*([^\r\n]+?)[ \t]*$",
            str(markdown or ""),
        )
        if match and match.group(1).strip():
            team[key] = match.group(1).strip()
    total_tokens, models, model_name = _team_metrics(entries, team)
    parsed = _ai_challenge_blocks(markdown)
    used: set[tuple[str, int]] = set()
    sections: dict[str, list[str]] = {category: [] for category in CATEGORY_ORDER}
    for entry in entries:
        project = entry["project"]
        expected_name = _text(_metadata(project).get("name") or project.name, project.name)
        category = _category(_metadata(project))
        selected: tuple[str, str] | None = None
        for parsed_category, candidates in parsed.items():
            for index, block in enumerate(candidates):
                if (parsed_category, index) in used:
                    continue
                if block[0].casefold() == expected_name.casefold():
                    selected = block
                    used.add((parsed_category, index))
                    break
            if selected is not None:
                break
        if selected is None:
            for index, block in enumerate(parsed.get(category, [])):
                if (category, index) not in used:
                    if block[0] in {"题目一名称", "题目名称", "XXX"}:
                        continue
                    selected = block
                    used.add((category, index))
                    break
        if selected is None:
            body = _challenge_markdown(project, entry.get("completion"), entry.get("facts", []))
        else:
            body = re.sub(
                r"^####[ \t]+.+$",
                f"#### {_safe(expected_name)}",
                selected[1],
                count=1,
                flags=re.MULTILINE,
            ).strip()
        sections[category].append(_ensure_challenge_sections(body))
    return _render_template(
        title=_safe(team.get("title"), "比赛解题报告"),
        team_name=_safe(team.get("name"), "待填写"),
        rank=_safe(team.get("rank"), "待填写"),
        solved_count=len(entries),
        token_total=total_tokens,
        model_name=model_name,
        sections=sections,
    )
