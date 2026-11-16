#!/usr/bin/env bash
# 将上游仓库合并到当前分支。仅当冲突只涉及本 fork 的改动时自动解决，其余情况中止。
set -euo pipefail

upstream="${UPSTREAM_REPO:-N3urda/hongguoTV}"
fork="${FORK_REPO:?需要设置 FORK_REPO，例如 bkys/hongguoTV}"

updates_file="kotlin-tv/core/src/main/kotlin/com/hongguotv/core/AppUpdates.kt"
updates_test="kotlin-tv/core/src/test/kotlin/com/hongguotv/core/AppUpdatesTest.kt"

git fetch --no-tags "https://github.com/${upstream}.git" "main:refs/remotes/upstream/main"

if git merge --no-edit upstream/main; then
  echo "上游合并完成：无冲突"
  exit 0
fi

conflicts="$(git diff --name-only --diff-filter=U)"
if [ -z "$conflicts" ]; then
  echo "::error::上游合并失败，且没有可自动处理的冲突文件"
  git merge --abort || true
  exit 1
fi
echo "冲突文件："
echo "$conflicts"

unresolved=""
for file in $conflicts; do
  case "$file" in
    "$updates_file")
      # 取上游版本后重新应用本 fork 的更新仓库地址。
      git checkout --theirs -- "$file"
      sed -i -E "s|const val REPOSITORY = \"[^\"]*\"|const val REPOSITORY = \"${fork}\"|" "$file"
      if ! grep -q "const val REPOSITORY = \"${fork}\"" "$file"; then
        echo "::error::无法在上游新版本中重新应用更新仓库地址"
        git merge --abort || true
        exit 1
      fi
      if ! grep -q "UPSTREAM_REPOSITORY = \"${upstream}\"" "$file"; then
        perl -0pi -e "s|( *)const val REPOSITORY = \"${fork}\"|\$1private const val UPSTREAM_REPOSITORY = \"${upstream}\"\n\$1@Suppress(\"unused\")\n\$1const val REPOSITORY = \"${fork}\"|" "$file"
      fi
      git add -- "$file"
      ;;
    "$updates_test")
      # 测试断言改为引用 AppUpdate.RELEASES，避免再次跟随仓库地址变化。
      git checkout --theirs -- "$file"
      perl -0pi -e 's|assertEquals\("https://github\.com/[^"]*/releases/download/|assertEquals("\$\{AppUpdate.RELEASES\}/download/|' "$file"
      if ! grep -q 'AppUpdate.RELEASES}/download/' "$file"; then
        echo "::error::无法重新应用更新地址测试断言"
        git merge --abort || true
        exit 1
      fi
      git add -- "$file"
      ;;
    *)
      unresolved="$unresolved $file"
      ;;
  esac
done

if [ -n "${unresolved// /}" ]; then
  git merge --abort || true
  echo "::error::上游改动与本地自定义冲突，需人工合并：$unresolved"
  exit 1
fi

git commit --no-edit
echo "上游合并完成：已自动解决 fork 自有文件的冲突"
