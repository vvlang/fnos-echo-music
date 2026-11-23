# Echo 音乐传输（fnOS 原生应用 · 飞牛音乐伴侣）

专为 **飞牛私有云（fnOS）** + **飞牛音乐（trim.music）** + **飞傲（FiiO）/ 雪漫天（Snowsky）Echo（含 AIYeon 固件）及各类 USB 外挂存储无损播放器** 打造的原生 `.fpk` 桌面应用。

## 核心功能（v1.1.0）

1. **直接依托飞牛音乐库（只读、零侵入）**
   - 以 `mode=ro` 只读直连 `/var/apps/trim.music/var/db/music.db`，实时呈现全部曲目、专辑墙、歌手库、**我喜欢的**、**我的歌单**与原始文件夹目录，支持在线试听。
   - 同步模式绝不修改飞牛音乐数据库，也绝不改动 NAS 原始音频文件。
2. **完整携带飞牛刮削好的专辑封面、歌词与元数据**
   - **专辑封面（播放器专优）**：读取 `/var/apps/trim.music/meta/cover/` 刮削封面，自动将播放器不兼容的 **WebP / PNG / CMYK / 超大图** 转为 **500×500 基线 RGB JPEG**（可配 300/500/800/1000），**同时内嵌进音频副本 + 生成专辑目录 `folder.jpg`**。
   - **LRC 同步歌词**：按 `用户偏好 > 用户关联 > 飞牛云端刮削 > 伴随文件 > 文件内嵌` 优先级读取 `/var/apps/trim.music/meta/lyric-sqlite/lyric-0*.db`，自动应用歌词时间偏移，生成严格同名的 UTF-8 `.lrc` 文件（并可选内嵌标签）。
   - **规范标签同步**：可选将飞牛整理好的标题、歌手、专辑、专辑艺人、年份、曲目号、碟号、流派写入副本标签。
3. **Echo / 飞傲外挂存储智能识别与格式适配**
   - 自动扫描 `/vol00/*`（`SD_card`、`Internal_storage`、`RemovableDisk` 等），实时查看剩余空间、在线浏览/清理旧文件，传输完成自动 `sync` 安全刷盘。
   - 有声书库自动归入 `Audiobooks/`（适配 AIYeon 首页有声书断点续播）；支持 CUE 整轨自动无损分轨 FLAC；支持 WMA / APE / DFF / AIFF / DTS 自动 `ffmpeg` 无损转码 FLAC；同步歌单/喜欢自动导出 `.m3u8`。
4. **曲库智能去重（v1.1.0 新增）**
   - **精确哈希去重**：直接利用飞牛音乐文件哈希，100% 零误伤清理完全相同的冗余副本。
   - **同曲多音质 / 跨格式去重**：按「歌名 + 歌手 + 时长校验（±2s/3s/5s）」聚合，自动过滤垃圾标签与 CUE 整轨，智能推荐保留 **DSD / WAV / FLAC** 等最高音质版本。
   - **同名文件排查**：跨目录排查同名文件，每组强制至少保留 1 份，防止误删全部。
5. **曲库整理工具箱 + 可逆安全回收站（v1.1.0 新增）**
   - **安全回收站（`.echo_recycle`）**：所有去重清理默认先移入可逆回收站，支持随时一键原路恢复，确认无误后再彻底删除释放空间。
   - **目录规范归档**：按「歌手 / 专辑」先生成预览计划（原路径 → 目标路径）再执行移动，可选顺带补齐 `folder.jpg` / `.lrc` 并清理空文件夹。
   - **杂物与空目录体检清理**：一键清理 macOS `._*`、`.DS_Store`、`Thumbs.db`、空文件夹与孤立 `.lrc`。
   - **刮削资源补齐到 NAS**：一键将飞牛刮削封面与 `.lrc` 导出至真实音乐文件夹。

## 目录结构

```text
fnos-echo-music/
├── fpk/                 # fnOS .fpk 打包源码
│   ├── manifest
│   ├── ICON.PNG / ICON_256.PNG
│   ├── config/          # privilege (run-as: root) & resource
│   ├── cmd/             # main & 生命周期脚本
│   └── app/
│       ├── ui/          # 飞牛桌面 iframe 入口配置与图标
│       └── server/      # FastAPI 后端 + 无外部 CDN 依赖前端 SPA
├── scripts/
│   ├── gen_icons.py     # 生成应用图标
│   └── build_fpk.sh     # 一键打包跨架构离线 .fpk (x86_64 + ARM64)
└── tests/
    └── test_core.py     # 单元与端到端测试
```

## 安装与自行打包

- **直接安装**：在 [Releases](https://github.com/vvlang/fnos-echo-music/releases) 下载最新离线 `.fpk`，进入飞牛 Web 界面「应用中心」→ 右上角「手动安装」上传即可（内置 x86_64 与 ARM64 离线依赖，全程无需联网）。
- **自行打包**：在装有 `fnpack` 的飞牛 NAS 上运行：
  ```bash
  bash scripts/build_fpk.sh
  sudo appcenter-cli install-fpk dist/echo-music-sync-1.1.0.fpk
  ```
- **运行测试**：
  ```bash
  pytest -v tests/test_core.py
  ```
