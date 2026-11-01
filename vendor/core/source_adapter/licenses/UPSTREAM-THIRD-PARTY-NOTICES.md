# 第三方组件许可说明

本软件（红果短剧下载器）使用了以下第三方组件。各组件的版权归其各自作者所有，
本软件对这些组件的使用均遵循其原始许可证。

---

## 运行时依赖

| 组件 | 版本 | 许可证 | 项目地址 |
|---|---|---|---|
| Electron | 44.6.0 | MIT | https://github.com/electron/electron |
| React | 18.3.1 | MIT | https://github.com/facebook/react |
| React DOM | 18.3.1 | MIT | https://github.com/facebook/react |
| axios | 1.20.0 | MIT | https://github.com/axios/axios |

Electron 打包产物中已包含 Chromium 与 Node.js 的许可声明：

- `LICENSE.electron.txt`
- `LICENSES.chromium.html`

---

## 内置可执行程序

### Windows FFmpeg / FFprobe（GPLv3-or-later）

Windows x64 内置从固定源码编译的 FFmpeg 9.0.2 与 x264 b35605ace3ddf7c1a5d67a2eb553f034aef41d55，位于 `resources/bin/`。
x264 为唯一外部编解码静态库（GPLv2-or-later）；整体构建为 GPLv3-or-later。构建验证 x64 架构与系统 DLL 依赖。
完整 FFmpeg/x264 对应源码、准确构建脚本、配置日志、工具链版本和二进制哈希随 Release 的 Windows 对应源码包提供。
许可全文与来源说明见 `COPYING.GPLv3`、`COPYING.GPLv2`、`LICENSE.md`、`WINDOWS-X264-LICENSE.txt` 和 `WINDOWS-NOTICE.txt`。
该精简构建提供软件 H.264 编码，不包含旧版 Gyan essentials 包及其额外库。

---

### Mac FFmpeg / FFprobe（GPLv3-or-later）

Apple Silicon 包内置从未修改源码编译的 FFmpeg 9.0.2 与 x264 b35605ace3ddf7c1a5d67a2eb553f034aef41d55，文件位于 `Contents/Resources/bin/`。
x264 是唯一非系统静态依赖（GPLv2-or-later），整体构建为 GPLv3-or-later；完整对应源码与准确构建脚本随 Release 提供。
准确来源、版本、构建配置及对应源码要求见 `build/ffmpeg/MACOS-NOTICE.txt`、
`MACOS-VERSIONS.txt` 和 `MACOS-LICENSE.txt`。

## 开发依赖

构建期使用以下工具（不随软件分发）：

| 组件 | 许可证 |
|---|---|
| Vite | MIT |
| electron-builder | MIT |
| @vitejs/plugin-react | MIT |
| concurrently | MIT |
| wait-on | MIT |

---

## 本软件自身

本软件基于上游项目二次开发，整体以 **GNU General Public License v3.0** 发布，
详见仓库根目录的 `LICENSE` 与 `NOTICE`。

## App 片源签名及 Spade 算法

改编自 woshishiq1/drpys，经 N3urda/hongguoTV 的 vendor 源码核对，GPL-3.0。
完整来源与修改说明见 `src/native/licenses/NOTICE.txt`，许可见同目录 `LICENSE.drpys.txt`。

## 西饭接口参考

独立编写适配器；接口路径核对参考 [Hululu007/drpy-node](https://github.com/Hululu007/drpy-node/tree/295f2b7047e14122d542a7736cb931e81abcf85c)（GPL-3.0）。
未复制其中的会话标识或解锁流程。公开接口可访问不代表作品获得再分发授权；安装包不包含剧集媒体。


## Anime4K-WebGPU

Package: anime4k-webgpu 1.0.0
Project: https://github.com/Anime4KWebBoost/Anime4K-WebGPU

The preview uses its CNNx2M implementation for optional on-device 2x enhancement.

Copyright (c) 2012-2023 Scott Chacon and others

Permission is hereby granted, free of charge, to any person obtaining
a copy of this software and associated documentation files (the
"Software"), to deal in the Software without restriction, including
without limitation the rights to use, copy, modify, merge, publish,
distribute, sublicense, and/or sell copies of the Software, and to
permit persons to whom the Software is furnished to do so, subject to
the following conditions:

The above copyright notice and this permission notice shall be
included in all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND,
EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF
MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND
NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE
LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION
OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION
WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.
