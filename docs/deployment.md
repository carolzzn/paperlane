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
