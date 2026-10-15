# 致谢与许可

## 本项目

**NodePanel** —— 自建 sing-box 的节点管理面板（界面、文档与部署脚本）。

## 许可

本项目基于以下开源项目修改而来，按其许可证条款使用：

| 组件 | 许可证 | 用途 |
| --- | --- | --- |
| [iwantruncom/renrenVPN](https://github.com/iwantruncom/renrenVPN) | GPL-3.0 | 面板主体（Web 界面、CLI、安装器），本项目在其基础上修改 |
| [SagerNet/sing-box](https://github.com/SagerNet/sing-box) | GPL-3.0-or-later | 代理内核 |
| [FastAPI](https://github.com/fastapi/fastapi) | MIT | Web 框架 |
| [uvicorn](https://github.com/encode/uvicorn) | BSD-3-Clause | ASGI 服务器 |
| [Jinja](https://github.com/pallets/jinja) | BSD-3-Clause | 模板引擎 |
| [bcrypt](https://github.com/pyca/bcrypt) | Apache-2.0 | 密码哈希 |
| [python-multipart](https://github.com/Kludex/python-multipart) | Apache-2.0 | 表单解析 |
| [qrcode](https://github.com/lincolnloop/python-qrcode) | BSD | 二维码生成 |
| [Pillow](https://github.com/python-pillow/Pillow) | MIT-CMU | 图像输出 |
| [itsdangerous](https://github.com/pallets/itsdangerous) | BSD-3-Clause | 会话签名 |
| [grpcio](https://github.com/grpc/grpc) | Apache-2.0 | 读取内核流量统计 |
| [SQLite](https://www.sqlite.org/) | Public Domain | 数据库 |
| [acme.sh](https://github.com/acmesh-official/acme.sh) | GPL-3.0 | 证书签发 |

完整的第三方许可文本见 `LICENSE`（GPL-3.0）以及各上游项目自身的许可文件。

## 关于 GPL-3.0 的要求

本项目整体以 **GPL-3.0** 分发。若你分发本项目或其修改版本（包括镜像打包、
预编译产物、作为服务提供给他人使用），必须：

1. 保留本许可声明与版权信息；
2. 提供完整的源代码（包括你对上游的修改）；
3. 不得对使用者施加额外限制。

**仅自用、不对外分发**时没有上述义务。

## 上游致谢

面板主体源自 `iwantruncom/renrenVPN`（GPL-3.0）。本项目在其基础上增加了：
NAT/端口受限环境的部署适配、中转与住宅代理出口、Clash 订阅输出、
DNS-01 证书签发，以及界面重做。原始项目的作者与许可声明在此保留。
