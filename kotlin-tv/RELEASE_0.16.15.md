# 红果 TV 0.16.15

versionCode 36，沿用原发布证书。

移植上游 `tsukiyo-zero/hongguoTV` 0.1.40 的高清单集取源：播放时优先请求
`POST https://reading.snssdk.com/novel/player/video_model/v1/`，再回退到批量
`preload/v1`、`video_tab` 和 `fallback_api`。该地址在部分剧集能返回 1080P
档位，修复批量接口只返回低档时一直播放低清的问题。

同时新增 tsukiyo 上游播放源监控：每小时检查上游播放相关文件，发现变化后
自动生成分析 PR；代码合并并递增版本后，由现有发布工作流构建。

## 安装包

- Android 6+ 通用版（android6）
- Android 6+ ARM32 版（android6-arm32-v7a）
- Android 6+ ARM64 版（android6-arm64-v8a）
- Android 5 兼容版（android5，包含 FFmpeg AAC 解码支持）

四种 APK 均可用原签名覆盖安装。新版更新器按 Android 系统版本选择兼容包或通用包，旧版更新器会先经兼容包升级。发布附件含对应源码、`update.json` 与 SHA256 校验文件。

## 验证

- `:core:test` 全部通过；新增单集 `video_model/v1` 首选、HTTP 503 后回退 `preload/v1` 的单元测试。
- 上游 0.1.40 已在实网验证 `video_model/v1` 返回高清档；本仓库首次发布前仍需在实体电视或模拟器确认真实高清片源与回退路径。
- 上游监控脚本本地运行通过；GitHub Actions 首次运行后检查分析 PR 是否按预期创建。
