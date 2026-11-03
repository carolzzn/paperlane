# 部署与维护

## 数据权限

先运行 `sh scripts/prepare.sh`。脚本仅在 `.env` 不存在时生成配置，并用当前用户的 UID/GID 设置新配置。不会覆盖已有 `.env`。默认工作目录为 `photos`，数据目录为 `data`；如果修改了自定义路径，请自行创建对应目录。

容器对待打印目录需要读取、遍历和移动权限，对已完成目录需要创建权限，对 data 目录需要读写权限。部分 NAS 还有厂商私有 ACL，仅修改 POSIX mode 不一定生效。请只调整本服务的专用目录，不要递归改动整个原图库或整个 Docker 共享目录。

为便于原子归档，两个照片目录在同一 `/print-workspace` 挂载中。不要把已完成目录单独挂载到另一块盘，或者让两个目录互相包含。

## 国内网络

如官方 Python 包源下载缓慢，可在 `.env` 将 `PIP_INDEX_URL` 改为 `https://pypi.tuna.tsinghua.edu.cn/simple`，再重新构建。此设置只作用于镜像依赖安装，不会上传照片。

## 日常维护

查看状态：

```sh
docker compose ps
```

查看日志：

```sh
docker compose logs --tail 100 photo-print
```

更新前先等待没有正在发送或等待出片的任务，再执行：

```sh
git pull --ff-only
```

```sh
docker compose up -d --build
```

停止服务不会删除文件、数据库和照片：

```sh
docker compose stop
```

建议一起备份 `.env`、`data` 和 `photos`。运行中直接复制 SQLite 文件可能得到不一致的副本；简单的备份方式是先停止本服务，备份这三个目录/文件，再启动。不要上传备份、初始密码、凭据哈希、任务数据库或个人照片到公开仓库。

## 密码与升级

新安装默认密码为 `password`；登录后通过“设置”自定义修改。首次改密成功会移除 data/access-password.txt 初始明文文件，并保留 data/credentials.json 中的盐与 scrypt 哈希。重新启动或升级不会重置已设置的密码。

旧版本已有 access-password.txt 时，升级自动把原密码迁移为哈希，原密码继续有效，直到你在网页中修改。修改后所有旧会话失效，当前浏览器获得新会话。服务重启也会清空登录会话，需要重新登录。

忘记密码时，需要有 NAS / Docker 管理权限的操作者处理：先停止本服务，备份 data，再删除 data/credentials.json 以及残留的 data/access-password.txt，重新启动后使用默认密码 `password` 登录。不要删除任务数据库或照片。不要在正在打印时进行此操作。

## 反向代理与快捷方式

网页端口可通过 `.env` 配置。对外访问请使用你自己的 HTTPS 反向代理，保留本服务密码登录；代理只需指向容器映射的 HTTP 端口。不需要暴露打印机的 631 端口。

iPhone：Safari 打开网页 → 分享 → 添加到主屏幕。Mac 可使用 Safari“添加到程序坞”（系统支持时）。图标与 Manifest 随服务提供，不依赖第三方 CDN。

## 支持范围

自动归档依赖 Linux 的 `renameat2(RENAME_NOREPLACE)`，目标必须与源在同一文件系统。推荐在 Linux NAS 或 Linux Docker 容器中运行；原生 Windows/macOS Python 服务的自动归档不受支持。

打印机必须接受 JPEG、相应纸张规格和 IPP 打印参数，并可靠上报任务状态。当前只对富士小俏印 2Pro 做过协议/无出纸验证，其他型号需要自行验证，实际出片不由测试通过推定。

## 从 v0.1.0 升级

仓库更名为 `carolzzn/paperlane`。现有克隆可更新 remote：

```sh
git remote set-url origin https://github.com/carolzzn/paperlane.git
```

先等打印任务结束并核对 unknown 任务，再停止服务，备份整个 `data`、配置和照片目录。保留原 Compose 项目名称与挂载路径，更新源码并构建。不要为了更名删除容器数据卷，不要用空目录替换 data。自定义部署的镜像标签可以调整，但 Compose 项目与数据路径无需更名。

首次迁移从原 `PRINTER_URI` 导入一台稳定 ID 的设备，历史任务归属这台设备。之后设备设置保存在 data/library.sqlite3，修改环境变量不会覆盖已有设置。旧预览明确过期，旧版待发送任务会取消；正在发送/等待中的任务标为 unknown，不自动重发。应在停机前等待全部实际打印结束。

密码哈希、历史与归档记录保留。登录会话不跨服务重启持久化，需要用原自定义密码重新登录。保留旧镜像与升级前备份，若需回滚，停止新服务后恢复备份 data 并切回旧镜像；不要让旧代码直接使用已经迁移的新数据库。

## 尺寸和其他品牌

先选择设备、实际相纸与分辨率，再选照片排版。自定义照片以毫米换算，按设备 X/Y 分辨率独立渲染。额外外边距在设备必需边距之外增加，不能减掉机械边距。不同相纸/边距/进纸源是实际报告的组合；最终仍需 Validate-Job 严格校验。

设备添加只查询能力，不打印。标尺页也是先预览，再由用户确认打印。使用直尺量 10 mm 方框及 0–50 mm 横线；不一致时记录具体机型、纸型、边距、分辨率和实测值，暂不认定尺寸准确。v0.2.0 不提供未经测量的自动补偿。

其他品牌应支持 JPEG、完整 IPP 参数校验和可靠的完成状态。仅支持 URF/PWG Raster、USB 或厂商私有协议的设备暂不可直接使用；CUPS 后端属于后续工作。默认安装不需要 host 网络或 Docker socket。
