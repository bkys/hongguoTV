#!/usr/bin/env python3
"""Fork 发布工具：校验 Gradle 产物、生成 update.json 并发布到本仓库 Release。

只处理 Android 6+（API 23）三个安装包，不构建 Android 5 兼容包与 FFmpeg。
"""
import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = "com.hongguotv.nativeapp"
GRADLE = ROOT / "kotlin-tv/app/build.gradle.kts"
APK_DIR = ROOT / "kotlin-tv/app/build/platform/outputs/apk/release"
OUT = ROOT / "outputs"
MIN_SDK = 23

# (Gradle 输出文件, Release 资源后缀, ABI filter)
VARIANTS = (
    ("app-universal-release.apk", "android6", None),
    ("app-armeabi-v7a-release.apk", "android6-arm32-v7a", "armeabi-v7a"),
    ("app-arm64-v8a-release.apk", "android6-arm64-v8a", "arm64-v8a"),
)

UPSTREAM = "N3urda/hongguoTV"
INSTALL = (
    "本 fork 仅提供 Android 6.0+（API 23）安装包：通用包、ARM32、ARM64，同一证书签名，"
    "可与后续版本覆盖安装；不明确 ABI 时用通用包。"
    "本包由 bkys/hongguoTV 使用自有证书构建，仅限个人研究使用，请勿传播或用于商业用途。"
)


def fail(message):
    print(f"::error::{message}", file=sys.stderr)
    raise SystemExit(1)


def run(*args):
    return subprocess.check_output([str(a) for a in args], cwd=ROOT, text=True).strip()


def sha256(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def source_version():
    text = GRADLE.read_text()
    version = re.search(r'versionName\s*=.*\?:\s*"([^"]+)"', text)
    code = re.search(r'versionCode\s*=.*\?:\s*(\d+)', text)
    if not version or not code:
        fail("无法从 kotlin-tv/app/build.gradle.kts 读取版本号")
    return version.group(1), int(code.group(1))


def build_tools():
    sdk = os.environ.get("ANDROID_HOME") or os.environ.get("ANDROID_SDK_ROOT")
    if not sdk:
        fail("缺少 ANDROID_HOME / ANDROID_SDK_ROOT")
    path = Path(sdk) / "build-tools/36.0.0"
    if not path.is_dir():
        fail("缺少 build-tools 36.0.0")
    return path


def tool(name):
    base = build_tools() / name
    for candidate in (base, base.with_name(name + ".exe"), base.with_name(name + ".bat")):
        if candidate.is_file():
            return candidate
    fail(f"缺少 build-tools 工具 {name}")


def check_metadata(path, abi, version, code):
    listing = path.parent / "output-metadata.json"
    if not listing.is_file():
        fail(f"{path.name} 缺少 output-metadata.json")
    elements = json.loads(listing.read_text()).get("elements", [])
    matches = [item for item in elements if item.get("outputFile") == path.name]
    expected = [dict(filterType="ABI", value=abi)] if abi else []
    if (len(matches) != 1 or matches[0].get("filters") != expected
            or matches[0].get("versionCode") != code
            or matches[0].get("versionName") != version):
        fail(f"{path.name} 的 ABI 分包或版本信息与当前构建不一致")


def check_apk(path, version, code, abi):
    badging = run(tool("aapt"), "dump", "badging", path)
    package = re.search(r"^package: name='([^']+)' versionCode='([^']+)' versionName='([^']+)'", badging, re.M)
    if not package or package.groups() != (PACKAGE, str(code), version):
        fail(f"{path.name} 的包名或版本与 build.gradle.kts 不一致")
    minimum = re.search(r"^sdkVersion:'(\d+)'", badging, re.M)
    if not minimum or minimum.group(1) != str(MIN_SDK):
        fail(f"{path.name} 的 minSdk 不是 {MIN_SDK}")
    signature = run(tool("apksigner"), "verify", "--verbose", "--min-sdk-version", str(MIN_SDK),
                    "--print-certs", path)
    certs = re.findall(r"^Signer #\d+ certificate SHA-256 digest: ([0-9a-fA-F]+)$", signature, re.M)
    if not certs:
        fail(f"{path.name} 未签名")
    expected = os.environ.get("HONGGUOTV_CERT_SHA256", "").strip().lower()
    if expected and [c.lower() for c in certs] != [expected]:
        fail(f"{path.name} 的签名证书与 HONGGUOTV_CERT_SHA256 不一致，已阻止发布")
    if not re.search(r"^Verified using v1 scheme .*: true$", signature, re.M):
        fail(f"{path.name} 缺少 v1 签名，API 23 设备无法安装")
    with zipfile.ZipFile(path) as package_zip:
        native = [name for name in package_zip.namelist() if name.startswith("lib/") and name.endswith(".so")]
    if native:
        fail(f"{path.name} 不应包含原生库：{native}")
    return certs[0].lower()


def version_key(tag):
    match = re.fullmatch(r"kotlin-v(\d+(?:\.\d+)*)", tag)
    if not match:
        fail(f"无法识别的版本标签：{tag}")
    return tuple(int(part) for part in match.group(1).split("."))


def releases(repo):
    return json.loads(run("gh", "release", "list", "--repo", repo, "--limit", "200",
                          "--json", "tagName,isDraft"))


def repo_name():
    repo = os.environ.get("GITHUB_REPOSITORY")
    if not repo or "/" not in repo:
        fail("缺少 GITHUB_REPOSITORY")
    return repo


def upstream_notes(version):
    """取上游同版本 Release 的说明；fork 修订版（四段版本号）回退到基础版本的说明。"""
    candidates = [version]
    if len(version.split(".")) > 3:
        candidates.append(version.rsplit(".", 1)[0])
    for candidate in candidates:
        url = f"https://api.github.com/repos/{UPSTREAM}/releases/tags/kotlin-v{candidate}"
        request = urllib.request.Request(url, headers={"User-Agent": "bkys-fork-release"})
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                body = json.load(response).get("body") or ""
        except Exception as error:
            print(f"无法读取上游 {candidate} 的发布说明：{error}")
            continue
        lines = (line.strip() for line in body.splitlines())
        kept = [line for line in lines
                if line and not line.startswith(("hongguoTV ", "构建要求", "源码提交：", "原发布证书签名"))]
        text = "\n\n".join(kept).strip()
        if text:
            return text, candidate
    return "", None


def previous_release_tag(version):
    tags = [tag for tag in run("git", "tag", "--list", "kotlin-v*").split()
            if re.fullmatch(r"kotlin-v\d+(?:\.\d+)*", tag)]
    current = version_key(f"kotlin-v{version}")
    older = sorted((tag for tag in tags if version_key(tag) < current),
                   key=version_key, reverse=True)
    for tag in older:
        if subprocess.run(["git", "merge-base", "--is-ancestor", tag, "HEAD"],
                          cwd=ROOT, capture_output=True).returncode == 0:
            return tag
    return None


def own_changes(previous):
    """本版本相对上一个发布的提交里，属于本 fork 而非上游的那些。"""
    if not previous:
        return []
    args = ["git", "log", f"{previous}..HEAD", "--no-merges", "--format=%s%x1f%b%x1e"]
    has_upstream = subprocess.run(["git", "rev-parse", "--verify", "--quiet", "upstream/main"],
                                  cwd=ROOT, capture_output=True).returncode == 0
    if has_upstream:
        args += ["--not", "upstream/main"]
    try:
        records = run(*args).split("\x1e")
    except subprocess.CalledProcessError:
        return []
    changes = []
    for record in records:
        subject, _, body = record.partition("\x1f")
        subject = subject.strip()
        # CI、构建、文档类提交与用户看到的功能无关，不写进版本介绍。
        if not subject or re.match(r"^(ci|build|chore|docs|style|test)(\(|:)", subject):
            continue
        change = chinese_change(subject, body)
        if change and change not in changes:
            changes.append(change)
    return changes[:8]


CJK = re.compile(r"[\u4e00-\u9fff]")
CONVENTION = re.compile(r"^(feat|fix|perf|refactor|release)(\([^)]*\))?:\s*", re.I)


def chinese_change(subject, body):
    """版本介绍只用中文：优先中文提交标题，其次正文里的“说明：”行。"""
    text = CONVENTION.sub("", subject).strip()
    if CJK.search(text):
        return text
    for line in body.splitlines():
        line = CONVENTION.sub("", line.strip()).strip()
        for marker in ("说明：", "说明:"):
            if line.startswith(marker):
                line = line[len(marker):].strip()
                if CJK.search(line):
                    return line
    return "其他细节优化"


def compose_notes(version, code):
    """拼出上游说明 + 本版本自有改动的介绍。

    返回（GitHub Release 用全文, update.json 里给应用内更新弹窗用的摘要）。
    """
    upstream, source = upstream_notes(version)
    changes = own_changes(previous_release_tag(version))
    if upstream:
        upstream_block = f"上游 {UPSTREAM} {source} 更新：\n{upstream}"
    elif len(version.split(".")) > 3:
        base = version.rsplit(".", 1)[0]
        upstream_block = f"本版基于上游 {base} 的自有改动修订，上游没有对应的 {version} 版本说明。"
    else:
        upstream_block = f"上游 {UPSTREAM} {version} 更新：上游未提供版本说明。"
    sections = [
        f"bkysHGTV {version}（versionCode {code}），Android 6.0+（API 23）。",
        upstream_block,
    ]
    if changes:
        sections.append("本版本自定义改动：\n" + "\n".join(f"- {change}" for change in changes))
    sections.append(INSTALL)

    if upstream:
        lead = upstream.split("\n\n")[0]
        summary = f"上游 {source}：{lead}"
    else:
        summary = upstream_block
    if changes:
        summary += "\n本版本自定义改动：" + "；".join(changes)
    summary += "\n" + INSTALL
    if len(summary) > 1400:
        summary = summary[:1399] + "…"
    return "\n\n".join(sections), summary


def prepare(tag, version, code):
    if tag != f"kotlin-v{version}":
        fail(f"标签 {tag} 与版本 {version} 不一致")
    commit = run("git", "rev-parse", "HEAD")
    OUT.mkdir(exist_ok=True)
    for stale in OUT.iterdir():
        if stale.is_file():
            stale.unlink()
    assets = []
    for name, suffix, abi in VARIANTS:
        path = APK_DIR / name
        if not path.is_file():
            fail(f"缺少构建产物 {name}，请先执行 :app:assembleRelease")
        check_metadata(path, abi, version, code)
        check_apk(path, version, code, abi)
        target = OUT / f"hongguotv-kotlin-{version}-{suffix}.apk"
        shutil.copy2(path, target)
        assets.append(target)
    universal = assets[0]
    digest = sha256(universal)
    size = universal.stat().st_size
    notes, summary = compose_notes(version, code)
    manifest = dict(
        schema=1, packageName=PACKAGE, versionCode=code, versionName=version, minSdk=MIN_SDK,
        tag=tag, sourceCommit=commit,
        apk=dict(name=universal.name, size=size, sha256=digest),
        platformApk=dict(name=universal.name, size=size, sha256=digest),
        notes=summary,
    )
    (OUT / "update.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    (OUT / "SHA256SUMS").write_text("".join(f"{sha256(p)}  {p.name}\n" for p in (*assets, OUT / "update.json")))
    (OUT / "release-notes.md").write_text(
        f"{notes}\n\n"
        "安装包：`hongguotv-kotlin-%s-android6.apk`（通用）、`-android6-arm32-v7a.apk`（ARM32）、"
        "`-android6-arm64-v8a.apk`（ARM64）。\n\n源码提交：%s。\n" % (version, commit)
    )
    print(f"已生成 {len(assets)} 个安装包与 update.json，版本 {version} / {code}")


def publish(tag, version):
    if tag != f"kotlin-v{version}":
        fail(f"标签 {tag} 与版本 {version} 不一致")
    repo = repo_name()
    existing_releases = releases(repo)
    current = next((item for item in existing_releases if item["tagName"] == tag), None)
    if current and not current["isDraft"]:
        print(f"{tag} 已发布，保持现有资源不变")
        return
    for item in existing_releases:
        if item["isDraft"] or not item["tagName"].startswith("kotlin-v"):
            continue
        if version_key(item["tagName"]) >= version_key(tag):
            fail(f"新版本 {tag} 不高于已发布版本 {item['tagName']}，已阻止发布")
    notes = str(OUT / "release-notes.md")
    files = sorted(p for p in OUT.iterdir() if p.is_file() and p.name != "release-notes.md")
    if not files:
        fail("outputs/ 为空，请先执行 prepare")
    if current:
        print(f"复用草稿 Release {tag}")
    else:
        run("gh", "release", "create", tag, "--repo", repo, "--target",
            run("git", "rev-parse", "HEAD"), "--title", f"bkysHGTV {version}",
            "--notes-file", notes, "--draft")
    run("gh", "release", "upload", tag, "--repo", repo, *map(str, files), "--clobber")
    uploaded = json.loads(run("gh", "release", "view", tag, "--repo", repo, "--json", "assets,isDraft"))["assets"]
    sizes = {asset["name"]: asset["size"] for asset in uploaded}
    for path in files:
        if sizes.get(path.name) != path.stat().st_size:
            fail(f"上传校验失败：{path.name}")
    run("gh", "release", "edit", tag, "--repo", repo, "--draft=false", "--latest=true", "--notes-file", notes)
    if json.loads(run("gh", "release", "view", tag, "--repo", repo, "--json", "isDraft"))["isDraft"]:
        fail("Release 仍为草稿")
    latest = json.loads(run("gh", "release", "list", "--repo", repo, "--limit", "1", "--json", "tagName"))
    if latest[0]["tagName"] != tag:
        fail("Release 未成为 latest")
    print(f"已发布 {tag}")


def emit(values):
    lines = "".join(f"{key}={value}\n" for key, value in values.items())
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with open(path, "a", encoding="utf-8") as out:
            out.write(lines)
    else:
        print(lines, end="")


def plan(force):
    """决定本次是否发布，并给 Gradle 提供 versionName / versionCode。

    默认跟随上游版本：只在上游 versionName 尚未发布过时发布。
    force 用于把当前 main 立即发一版：版本号记作 <上游版本>.<序号>。
    versionCode 统一用 上游 versionCode * 100 (+序号)，保证跨版本单调递增。
    """
    base_version, base_code = source_version()
    repo = repo_name()
    tags = {item["tagName"] for item in releases(repo) if not item["isDraft"]}
    base_tag = f"kotlin-v{base_version}"
    version, code, release = base_version, base_code * 100, True
    if base_tag in tags:
        if force:
            index = 1
            while f"kotlin-v{base_version}.{index}" in tags:
                index += 1
            version = f"{base_version}.{index}"
            code = base_code * 100 + index
        else:
            release = False
    tag = f"kotlin-v{version}"
    if tag in tags:
        release = False
    emit(dict(release=str(release).lower(), version=version, code=code, tag=tag))
    print(f"计划：{'发布 ' + tag if release else '跳过发布'}（versionName {version} / versionCode {code}）")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["plan", "prepare", "publish"])
    parser.add_argument("tag", nargs="?")
    parser.add_argument("--version")
    parser.add_argument("--code", type=int)
    parser.add_argument("--force", default="false")
    args = parser.parse_args()
    if args.action == "plan":
        plan(args.force.strip().lower() == "true")
        return
    if not args.tag or not args.version or args.code is None:
        fail("prepare/publish 需要 <tag> --version 与 --code")
    if args.action == "prepare":
        prepare(args.tag, args.version, args.code)
    else:
        publish(args.tag, args.version)


if __name__ == "__main__":
    main()
