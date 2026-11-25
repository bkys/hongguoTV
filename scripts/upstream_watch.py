#!/usr/bin/env python3
"""监控 tsukiyo-zero/hongguoTV 的播放源变化，自动生成分析 PR。

本仓库与该上游没有共同 git 历史，不能直接 merge。脚本只做三件事：
1. 找出上次记录之后的播放源相关提交；
2. 提取上游/本地的播放地址、版本参数、签名常量并生成差异报告；
3. 在 CI 中创建/更新分析 PR，并记录已检查的上游提交。

真正的代码移植需在 PR 分支上完成；合并并递增版本后，由 fork-sync-release
工作流构建发布。脚本不会自动修改播放实现，也不会输出签名、Cookie 或媒体地址。
"""
import argparse
import datetime as dt
import json
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
UPSTREAM = "tsukiyo-zero/hongguoTV"
UPSTREAM_REF = "tsukiyo/main"
STATE = ROOT / ".github/upstream/tsukiyo.json"
REPORT = ROOT / "docs/upstream/tsukiyo-playback-watch.md"
LABEL = "upstream-watch"

# 会改变播放源地址、取源顺序或签名的文件。路径不存在时自动跳过。
PLAYBACK_FILES = (
    "kotlin-tv/core/src/main/kotlin/com/hongguotv/core/MobilePlayback.kt",
    "kotlin-tv/core/src/main/kotlin/com/hongguotv/core/PlaybackShapes.kt",
    "kotlin-tv/core/src/main/kotlin/com/hongguotv/core/PlaybackModels.kt",
    "kotlin-tv/core/src/main/kotlin/com/hongguotv/core/PlaybackRoutes.kt",
    "kotlin-tv/core/src/main/kotlin/com/hongguotv/core/PlaybackDeviceSession.kt",
    "kotlin-tv/core/src/main/kotlin/com/hongguotv/core/ContentRepository.kt",
    "kotlin-tv/core/src/main/kotlin/com/hongguotv/core/Signer.kt",
    "kotlin-tv/core/src/main/kotlin/com/hongguotv/core/V5Constants.kt",
    "kotlin-tv/core/src/main/kotlin/com/hongguotv/core/RemoteVideo.kt",
    "kotlin-tv/core/src/main/kotlin/com/hongguotv/core/PlayRetry.kt",
    "kotlin-tv/app/src/main/kotlin/com/hongguotv/nativeapp/PlaybackSources.kt",
    "kotlin-tv/app/src/main/kotlin/com/hongguotv/nativeapp/FeedPlayback.kt",
    "kotlin-tv/app/src/main/kotlin/com/hongguotv/nativeapp/RemoteVideo.kt",
)

URL = re.compile(r"https://[A-Za-z0-9._~:/?#@!$&()*+,;=%\[\]-]+")
FIELD21 = re.compile(r"field21\s*=\s*(\d+)")
PATH_SEGMENT = re.compile(r"(novel/player/[A-Za-z0-9_./-]+)")

# 版本号经常写成 if/else 或拼接，按“包含字段名的行”提取，避免漏掉条件分支里的值。
LINE_FIELDS = {
    "version_code": ("version_code", re.compile(r"\b7\d{4}\b")),
    "version_name": ("version_name", re.compile(r"\b7\.\d+\.\d+\.\d+\b")),
}

DIRECT_FIELDS = {
    "channel": re.compile(r'"channel"\s*(?:to|:)\s*"([^"]+)"'),
    "device_type": re.compile(r'"device_type"\s*(?:to|:)\s*"([^"]+)"'),
    "device_level": re.compile(r'"device_level"\s*(?:to|:)\s*"?(\d+)"?'),
    "dr_scene": re.compile(r'"dr_scene"\s*(?:to|:)\s*"([^"]+)"'),
}

INVENTORY_PATTERNS = {
    "播放/模型地址": URL,
    "模型路径": PATH_SEGMENT,
    "签名 field21": FIELD21,
}


def run(*args, check=True):
    result = subprocess.run(
        [str(arg) for arg in args], cwd=ROOT, text=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    if check and result.returncode != 0:
        raise SystemExit(f"命令失败：{' '.join(map(str, args))}\n{result.stderr.strip()}")
    return result.stdout.strip()


def git(*args, check=True):
    return run("git", *args, check=check)


def read_state():
    if not STATE.is_file():
        return {}
    try:
        return json.loads(STATE.read_text())
    except json.JSONDecodeError:
        return {}


def state_payload(head, version):
    return {
        "upstream": UPSTREAM,
        "lastCommit": head,
        "version": version,
        "checkedAt": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
    }


def write_state(head, version):
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(state_payload(head, version), ensure_ascii=False, indent=2) + "\n")


def upstream_head():
    return git("rev-parse", UPSTREAM_REF)


def upstream_version(ref):
    text = git("show", f"{ref}:kotlin-tv/app/build.gradle.kts", check=False)
    if not text:
        return ""
    version = re.search(r'versionName\s*=.*?\?:\s*"([^"]+)"', text)
    return version.group(1) if version else ""


def is_ancestor(older, newer):
    return subprocess.run(
        ["git", "merge-base", "--is-ancestor", older, newer], cwd=ROOT,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    ).returncode == 0


def commits_between(older, head):
    if older and is_ancestor(older, head):
        span = f"{older}..{head}"
        raw = git("log", "--format=%h%x1f%ci%x1f%s%x1e", span, "--", *PLAYBACK_FILES)
    else:
        # 首次运行或上游改写历史：只看最近 10 条播放源提交，避免全仓库 diff。
        raw = git("log", "-10", "--format=%h%x1f%ci%x1f%s%x1e", head, "--", *PLAYBACK_FILES)
    commits = []
    for record in raw.split("\x1e"):
        fields = record.strip().split("\x1f")
        if len(fields) == 3:
            commits.append(tuple(fields))
    return commits


def diff_files(older, head):
    if not older or not is_ancestor(older, head):
        return []
    raw = git("diff", "--name-status", f"{older}..{head}", "--", *PLAYBACK_FILES)
    return [line for line in raw.splitlines() if line.strip()]


def playback_diff(older, head, limit=45_000):
    if not older or not is_ancestor(older, head):
        return ""
    text = git("diff", f"{older}..{head}", "--", *PLAYBACK_FILES)
    if len(text) <= limit:
        return text
    return text[:limit] + "\n\n...（diff 过长，已截断）\n"


def inventory(text):
    values = {}
    for label, pattern in INVENTORY_PATTERNS.items():
        found = set()
        for match in pattern.finditer(text):
            value = (match.group(1) if match.groups() else match.group(0)).strip(" \t\"',;()")
            if label == "播放/模型地址":
                if not re.search(r"(snssdk|fqnovel|hongguo)", value):
                    continue
                if not ("player" in value or "video_tab" in value
                        or value.rstrip("/") == "https://reading.snssdk.com"):
                    continue
                if any(token in value for token in ("$", "{", "<", "example", "localhost", "127.0.0.1")):
                    continue
            if value:
                found.add(value)
        values[label] = found

    for label, (key, pattern) in LINE_FIELDS.items():
        found = set()
        for line in text.splitlines():
            if key not in line:
                continue
            for match in pattern.finditer(line):
                value = (match.group(1) if match.groups() else match.group(0)).strip(' \t"')
                if value and value != key:
                    found.add(value)
        values[label] = found

    for label, pattern in DIRECT_FIELDS.items():
        values[label] = {
            (match.group(1) if match.groups() else match.group(0)).strip(' \t"')
            for match in pattern.finditer(text)
        }
    return values


def read_sources(ref=None):
    texts = []
    for path in PLAYBACK_FILES:
        if ref:
            text = git("show", f"{ref}:{path}", check=False)
        else:
            file = ROOT / path
            text = file.read_text() if file.is_file() else ""
        if text:
            texts.append(text)
    return "\n".join(texts)


def compare(upstream, local):
    rows = []
    for label in (*INVENTORY_PATTERNS, *LINE_FIELDS, *DIRECT_FIELDS):
        added = sorted(upstream.get(label, set()) - local.get(label, set()))
        removed = sorted(local.get(label, set()) - upstream.get(label, set()))
        if added or removed:
            rows.append((label, added, removed))
    return rows


def render_report(head, version, older, commits, files, diff, comparison):
    now = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines = [
        "# tsukiyo-zero 上游播放源监控",
        "",
        f"- 检查时间：{now}",
        f"- 上游：`{UPSTREAM}`",
        f"- 上游版本：`{version or '未知'}`",
        f"- 上游提交：`{head}`",
        f"- 上次记录：`{older or '无'}`",
        "",
        "本仓库与该上游没有共同 git 历史，本 PR 只做播放源分析和移植清单，不自动合并代码。",
        "",
        "## 播放源相关提交",
        "",
    ]
    if commits:
        lines += ["| 提交 | 时间 | 说明 |", "| --- | --- | --- |"]
        lines += [f"| `{sha}` | {date} | {subject.replace('|', '\\|')} |" for sha, date, subject in commits]
    else:
        lines.append("没有新的播放源相关提交。")

    lines += ["", "## 变更文件", ""]
    if files:
        lines += [f"- `{line}`" for line in files]
    else:
        lines.append("- 无")

    lines += ["", "## 播放地址/参数差异", ""]
    if comparison:
        lines += ["| 项目 | 上游新增 | 本地新增 |", "| --- | --- | --- |"]
        for label, added, removed in comparison:
            lines.append(
                f"| {label} | {'、'.join(f'`{v}`' for v in added) or '—'} "
                f"| {'、'.join(f'`{v}`' for v in removed) or '—'} |"
            )
    else:
        lines.append("常量清单没有差异；仍需结合下方 diff 判断取源顺序和回退逻辑。")

    lines += [
        "",
        "## 移植清单",
        "",
        "- [ ] 对比上游新增/调整的播放地址、版本参数与签名常量",
        "- [ ] 在本仓库对应取源链路实现，并保留旧地址作为回退",
        "- [ ] 更新 `源地址说明.MD`",
        "- [ ] 补充 `MobilePlaybackTest` / `ContentRepository*Test`",
        "- [ ] 运行 `:core:test` 与 `:app:testDebugUnitTest`",
        "- [ ] 递增 `versionName` / `versionCode`，合并后由发布工作流构建",
        "",
        "## 上游播放源 diff",
        "",
    ]
    if diff:
        lines += ["<details><summary>展开 diff</summary>", "", "```diff", diff, "```", "", "</details>"]
    else:
        lines.append("无可展示的 diff。")
    lines.append("")
    return "\n".join(lines)


def create_pr(branch, report_text, head, version):
    title = f"chore(upstream): tsukiyo 播放源更新 {head[:10]}"
    run("gh", "label", "create", LABEL, "--color", "1D76DB",
        "--description", "tsukiyo-zero 播放源上游分析", "--force", check=False)
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(report_text)
    git("checkout", "-B", branch)
    git("add", str(REPORT.relative_to(ROOT)))
    git("commit", "-m", f"docs(upstream): 分析 tsukiyo 播放源 {head[:10]} [skip ci]")
    git("push", "-f", "-u", "origin", branch)
    existing = run("gh", "pr", "view", branch, "--json", "number,url", "--jq", ".number", check=False)
    if existing:
        run("gh", "pr", "edit", branch, "--title", title, "--body-file", str(REPORT))
        print(f"已更新分析 PR #{existing}")
    else:
        created = subprocess.run(
            ["gh", "pr", "create", "--base", "main", "--head", branch,
             "--title", title, "--body-file", str(REPORT), "--label", LABEL],
            cwd=ROOT, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        if created.returncode == 0:
            print(f"已创建分析 PR：{created.stdout.strip()}")
        else:
            print(f"创建 PR 失败，改为创建 Issue：{created.stderr.strip()}")
            run("gh", "issue", "create", "--title", title, "--body-file", str(REPORT), "--label", LABEL)


def record_state(head, version):
    git("checkout", "main")
    git("pull", "--rebase", "origin", "main", check=False)
    write_state(head, version)
    git("add", str(STATE.relative_to(ROOT)))
    committed = subprocess.run(
        ["git", "commit", "-m", f"chore(upstream): 记录 tsukiyo 播放源监控 {head[:10]} [skip ci]"],
        cwd=ROOT, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    if committed.returncode == 0:
        push = subprocess.run(["git", "push", "origin", "main"], cwd=ROOT,
                              text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if push.returncode != 0:
            print(f"状态推送失败：{push.stderr.strip()}", file=sys.stderr)
    else:
        print("状态没有变化，无需提交")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--force", action="store_true", help="忽略上次记录，强制分析当前上游")
    args = parser.parse_args()

    if not git("rev-parse", "--verify", "--quiet", UPSTREAM_REF, check=False):
        raise SystemExit(f"缺少 {UPSTREAM_REF}，请先 git fetch {UPSTREAM} main")

    head = upstream_head()
    version = upstream_version(head)
    state = read_state()
    older = "" if args.force else state.get("lastCommit", "")
    if older == head:
        print(f"上游未变化：{head}")
        return

    commits = commits_between(older, head)
    files = diff_files(older, head)
    comparison = compare(inventory(read_sources(head)), inventory(read_sources()))
    report = render_report(head, version, older, commits, files,
                           playback_diff(older, head), comparison)

    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(report)
    print(f"已生成报告：{REPORT.relative_to(ROOT)}")

    if os.environ.get("GITHUB_ACTIONS") != "true":
        write_state(head, version)
        print("本地运行：已更新状态，未创建 PR")
        return

    if not commits and not files:
        print("只有版本/其他变化，没有播放源改动；仅记录状态")
        record_state(head, version)
        return

    create_pr(f"upstream/tsukiyo-{head[:10]}", report, head, version)
    record_state(head, version)


if __name__ == "__main__":
    main()
