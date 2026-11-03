# 拾光印 · Shiguang Print

把 NAS 文件夹里的照片排版、打印，再自动归档。

Self-hosted photo printing for your NAS: folder-based queues, browser layouts, IPP printing, and automatic archiving.

下一版计划更名为“纸间 Paperlane”，并研究精确尺寸与多打印机能力协商。详见 [v0.2.0 设计方案](docs/roadmap-v0.2.0.md)；这些新增能力尚未发布。

> **v0.1.0 预览版。** 已在富士小俏印 2Pro 上完成 IPP 能力与无出纸参数校验，并通过模拟打印机和合成照片测试；首张实际出片效果仍待验证。其他打印机尚未验证。本项目与富士胶片没有隶属关系。

## 工作流程

```text
手动上传照片到「待打印」
          ↓
浏览器选片 → 排版预览 → 确认打印
          ↓
打印机报告该页所有份数完成
          ↓
源照片自动移入「已完成 / 完成日期」
```

- 支持 JPEG、PNG、HEIC、HEIF；自动处理方向和颜色配置，视频不进入队列。
- 统一使用 6 寸相纸；提供单张、双拼、四宫格、九宫格，以及在 6 寸相纸上排两张 3 寸方形照片的裁切模板。
- 支持完整保留或铺满裁剪、逐张旋转和构图、外边距、间距、底色、每页份数；按勾选顺序分页，空格留空。
- 预览与发送使用同一份 JPEG，不改写待打印源照片。
- 打印失败、取消或发送结果不确定时保留源文件，不自动重发；多份全部完成才归档。
- 归档前校验文件哈希，原子移动且不覆盖目标，支持归档中断恢复。
- 默认密码 `password`，网页“设置 → 修改访问密码”；密码使用 scrypt 哈希保存，修改后其他会话失效。
- 响应式网页，带 iPhone 主屏幕图标和 Web App Manifest。

## 快速部署

需要 Linux Docker Engine、Docker Compose 插件，以及支持 `image/jpeg` 的局域网 IPP 打印机。自动移动要求待打印与已完成目录位于同一文件系统、同一 Docker 挂载中。

1. 克隆项目并初始化配置：

```sh
git clone https://github.com/carolzzn/shiguang-print.git
```

```sh
cd shiguang-print
```

```sh
sh scripts/prepare.sh
```

2. 编辑 `.env`，把 `PRINTER_URI` 改成自己打印机的真实 IPP 地址，例如 `ipp://printer.local:631/ipp/print`。确认 `PUID`、`PGID` 是可读写工作目录的用户和组。端口默认 `18318`，可自行修改。

3. 构建并启动：

```sh
docker compose up -d --build
```

4. 通过 NAS 地址及配置端口打开网页，默认密码是 **`password`**。登录后在“设置 → 修改访问密码”中自定义密码。改密成功后，初始密码文件会删除；新密码只保存哈希，不能查看明文。后续升级会保留已设置的密码，不会重置为默认值。

5. 用已有的 NAS 文件管理或 SMB 上传照片到 `photos/待打印`，在网页刷新列表、选片、预览并打印。完成文件在 `photos/已完成/日期`。建议先用一张、6 寸、1 份验证实际纸张边缘和色彩。

本项目提供源码构建的 Docker Compose，首次部署不依赖预构建镜像。仓库还包含可由版本标签触发的 GitHub Actions 镜像构建工作流，目标架构为 `linux/amd64`、`linux/arm64`；ARM 的实机打印未验证。

## 文件与配置

```text
shiguang-print/
├── .env                  # 你自己的地址、端口和目录配置，不提交 Git
├── compose.yaml
├── data/                 # 密码哈希、任务记录、索引、临时预览
└── photos/
    ├── 待打印/           # 手动放入准备打印的副本
    └── 已完成/           # 确认打印完成后自动移动至此
```

不需要连接 iCloud，也不会访问挂载目录之外的图库。请把想打印的照片复制进待打印文件夹。网页不提供上传功能，继续使用你的 NAS 文件管理工具即可。

| 配置项 | 用途 |
| --- | --- |
| `PRINTER_URI` | 打印机 IPP URI；留空时仍可浏览、预览，不能打印 |
| `PRINTER_NAME` | 打印机离线时的显示名称 |
| `PORT` | NAS 上的网页端口，默认 18318 |
| `PUID` / `PGID` | 容器进程用户、组，须匹配目录权限 |
| `PHOTO_WORKSPACE` | 打印工作目录，默认 `./photos` |
| `DATA_DIRECTORY` | 本服务数据目录，默认 `./data` |
| `PIP_INDEX_URL` | 构建时的 Python 包索引，可改国内镜像 |
| `TZ` | 日期与归档时区，默认 `Asia/Shanghai` |

更多说明见 [部署与维护](docs/deployment.md)、[安全与数据范围](SECURITY.md)、[版本记录](CHANGELOG.md)。

## 打印与归档的边界

- “打印机已接收”不等于完成，必须等 IPP `job-state=completed`。
- 发送超时、服务在发送中重启等情况可能已经出片，系统会暂停队列，避免自动重复打印。核对打印机队列和相纸后，手动结束本批。
- 同一拼版页的所有份数完成后，才归档这一页涉及的所有源照片。
- 照片在预览后被修改、被替换，或归档权限/空间不足时，不擅自删除待打印文件。具体状态在打印记录中显示。
- 3 寸方形双拼是在 **4 × 6 英寸相纸上排两张 3 × 3 英寸照片**，需自行裁切，不代表打印机自动切纸，也不启用独立小相纸模式。
- 固定纸张、300 dpi、JPEG、无边距相关参数针对当前验证设备。其他型号不能仅凭支持 AirPrint 就视为兼容。

## 开发与测试

Python 3.11，安装依赖后运行：

```sh
PYTHONPATH=app python -m unittest discover -s tests -v
```

测试仅在临时目录中生成合成照片并使用模拟打印机，不会联系真实打印机或消耗相纸。请只运行单个 Uvicorn worker；登录会话和打印队列调度是单进程设计，不支持多个副本同时管理同一份工作目录。

## License

[MIT](LICENSE)。
