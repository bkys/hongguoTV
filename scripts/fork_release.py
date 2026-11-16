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

NOTES = (
    "同步上游 N3urda/hongguoTV 源码，由 bkys/hongguoTV 使用自有发布证书自动构建。"
    "提供 Android 6.0+（API 23）通用包、ARM32 与 ARM64 安装包；不含 Android 5 兼容包。"
    "应用内更新已指向本仓库 Release，可直接检查并覆盖安装。"
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
    match = re.fullmatch(r"kotlin-v(\d+)\.(\d+)\.(\d+)", tag)
    if not match:
        fail(f"无法识别的版本标签：{tag}")
    return tuple(int(part) for part in match.groups())


def releases(repo):
    return json.loads(run("gh", "release", "list", "--repo", repo, "--limit", "200",
                          "--json", "tagName,isDraft"))


def repo_name():
    repo = os.environ.get("GITHUB_REPOSITORY")
    if not repo or "/" not in repo:
        fail("缺少 GITHUB_REPOSITORY")
    return repo


def prepare(tag):
    version, code = source_version()
    if tag != f"kotlin-v{version}":
        fail(f"标签 {tag} 与源码版本 {version} 不一致")
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
    manifest = dict(
        schema=1, packageName=PACKAGE, versionCode=code, versionName=version, minSdk=MIN_SDK,
        tag=tag, sourceCommit=commit,
        apk=dict(name=universal.name, size=size, sha256=digest),
        platformApk=dict(name=universal.name, size=size, sha256=digest),
        notes=NOTES,
    )
    (OUT / "update.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    (OUT / "SHA256SUMS").write_text("".join(f"{sha256(p)}  {p.name}\n" for p in (*assets, OUT / "update.json")))
    (OUT / "release-notes.md").write_text(
        f"红果 TV {version}（versionCode {code}），Android 6.0+（API 23）。\n\n{NOTES}\n\n"
        f"源码提交：{commit}。\n\n"
        "安装包：`hongguotv-kotlin-%s-android6.apk`（通用）、`-android6-arm32-v7a.apk`（ARM32）、"
        "`-android6-arm64-v8a.apk`（ARM64）。三者同一证书签名，可与后续版本覆盖安装；"
        "不明确 ABI 时用通用包。\n" % version
    )
    print(f"已生成 {len(assets)} 个安装包与 update.json，版本 {version} / {code}")


def publish(tag):
    version, _ = source_version()
    if tag != f"kotlin-v{version}":
        fail(f"标签 {tag} 与源码版本 {version} 不一致")
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
            run("git", "rev-parse", "HEAD"), "--title", f"红果 TV {version}",
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


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["prepare", "publish"])
    parser.add_argument("tag")
    args = parser.parse_args()
    if args.action == "prepare":
        prepare(args.tag)
    else:
        publish(args.tag)


if __name__ == "__main__":
    main()
