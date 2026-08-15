# Media Luna (AstrBot 移植版)

按照 Koishi `koishi-plugin-media-luna` 的结构复现到 AstrBot：

- 24 个连接器（渠道）：
  - 生图：DALL-E / OpenAI、Chat API（兼容接口）、Gemini、SD WebUI、ComfyUI、Flux(Replicate)、Midjourney(Proxy)、Stability、豆包 Seedream、NovelAI、Pollinations、Agnes Image、BytePlus Seedream、Custom Form Image、ModelScope（魔搭）、Peinture（派奇智图）、Vertex AI、测试（本地生成）
  - 视频：OpenAI Video (Sora)、Agnes Video、NewAPI Video、Runway、智谱 CogVideoX
  - 音频：Suno AI 音乐
- **每个渠道可自定义**：请求地址、API Key、模型、输出尺寸/宽高比、步数、采样器、负面提示词等，字段随连接器动态变化
- **在线预设**：拉取 Prompt-Manager 在线模板，自动生成关键词（类型标签 + 模板标签）；预设支持自定义触发词、提示词（可用 `{prompt}` 占位符）、参考图、缩略图
- **保存目录可配置**：`save_dir` 留空默认保存在插件目录下，也可指向任意目录
- 简单用量统计（`/stats`、`/mystats`）、任务记录（`/tasks`、`/taskinfo`），不做积分计费
- 全量设置页面**内嵌于 AstrBot 主 Dashboard**（`/api/plug/media_luna/page/dashboard`），无需额外端口

## 安装

将本目录放到 `R:\AstrBot\data\plugins\` 下，重启 `astrbot run` 或在 Dashboard 插件管理中重载。

## 聊天指令

所有命令使用唤醒前缀（默认 `/`）：

| 命令 | 说明 |
|------|------|
| `/<渠道> [预设] <提示词>` | 渠道名即指令，如 `/nano 一只猫`。纯视频渠道自动生成视频，其他默认生成图片 |
| `/video <渠道> [预设] <提示词>` | 显式指定生成视频（如 ComfyUI 渠道生成视频时使用） |
| `/redraw <任务ID>`（别名 `/重新生成 <任务ID>`） | 复刻指定任务，用相同渠道和提示词重新生成 |
| `/channels` | 查看渠道及其模型 |
| `/preset list` | 查看预设（触发词 / 关键词 / 来源） |
| `/preset sync` | 立即同步在线预设 |
| `/preset add <触发词> <提示词>` | 新建预设（关键词/参考图/缩略图到 WebUI 补充） |
| `/preset del <触发词>` | 删除预设 |
| `/tasks [数量]`、`/taskinfo <ID>` | 任务记录 |
| `/stats`、`/mystats` | 用量统计 |

消息中附带的图片会作为参考图（图生图）传给支持该能力的连接器（SD WebUI / Gemini / 豆包 / Stability / ComfyUI）。另外：

- **@ 用户**：指令消息里 @ 的每个用户，其**头像**会被拉取作为参考图。
- **引用消息**：会读取**被引用消息内的图片**作为参考图（只取图片，引用里的文字等其他内容一律忽略）。
- 收集顺序：预设参考图 → 引用消息图片 → @ 用户头像 → 当前消息附带图片。

> 渠道指令是插件启动时按 `channels.json` 动态注册的真实指令（受唤醒前缀约束），在 WebUI 增删/启停渠道后自动重新注册，无需重启。

## 设置页面（内嵌于主 Dashboard）

插件页面通过 AstrBot 官方插件页面机制挂载在主 Dashboard（端口 6185）上，**不需要额外端口**，共两个独立页面：

- **dashboard**：渠道 / 预设 / 在线预设 / 任务 / 统计
- **config**：全局配置（保存目录、超时、输出组合等）
- 访问方式（推荐）：打开 AstrBot Dashboard → 插件管理 → 找到 media_luna → 打开对应的页面入口（侧边栏会列出两个页面，可原生切换）。**请从 Dashboard 内打开**，刷新/导航由 Dashboard 管理。
- 直接地址仅临时使用：`http://<主机>:6185/api/plug/media_luna/page/dashboard`——注意该地址的访问令牌 60 秒过期，**直接刷新会导致页面失效（表现为数据消失/空白）**，请改用侧边栏入口打开。
- 鉴权：复用 Dashboard 登录（JWT），无需单独 Token。
- 页面功能：渠道管理、预设管理（详情/简略模式、拖拽上传、在线同步）、全局配置、任务详情、用量统计，全部与独立版一致。

> 说明：插件页 iframe 处于 sandbox 环境，原生 `confirm()` 弹窗会被浏览器静默拦截，因此页面内置了自定义确认对话框；删除渠道/预设请使用页面内的确认框。

> 注意：AstrBot 指令需要唤醒前缀（默认 `/`），例如 `/nano 画一棵树`，不带前缀的普通消息（`nano 画一棵树`）不会触发指令。

> 页面加载依赖 AstrBot v4.27+ 的插件页面桥接机制（`pages/` 目录 + metadata `pages` 声明）。旧版本的独立 WebUI 端口方案已移除。
- **渠道**：增删改渠道。连接器决定了可编辑字段（请求地址、尺寸/宽高比、模型、Key 等），页面按字段类型动态渲染；开关类字段均为「开启/关闭」下拉。
- **预设**：管理触发词、提示词模板、关键词（逗号分隔，渠道关键词与之匹配）、缩略图（URL 或本地上传/拖拽）、参考图（每行一个 URL 或上传/拖拽）。支持**详情模式**（表格）和**简略模式**（触发词+缩略图的框型卡片）两种视图。
- **在线预设**：配置 API 地址、自动同步、间隔、是否删除已下线预设，并支持一键同步。
- **配置**：保存目录（留空 = 插件目录）、超时、输出组合（任务ID/用时/模型是否显示）、WebUI 监听地址/端口。所有开关类配置均为「开启/关闭」下拉。
- **任务/统计**：支持手动刷新和自动刷新（默认 5 秒一次，可关闭）；点击任务行查看完整详情。
- 弹窗关闭：单击空白处不会关闭正在编辑的渠道/预设，**双击空白处**才关闭。

## 输出内容组合

生成结果是一条整合消息（不分开发送），可配置显示哪些部分（WebUI → 配置，或 `data/config/astrbot_plugin_media_luna_config.json`）：

- `output_show_task_id`：是否显示任务ID
- `output_show_elapsed`：是否显示生成总用时
- `output_show_model`：是否显示使用的模型

默认组合顺序：任务ID → 图片/视频 → 用时 → 模型。

## 在线预设说明

默认从 `https://prompt.vioaki.xyz/api/templates?per_page=-1` 拉取（Prompt-Manager 项目）。

每个模板同步后自动生成关键词：`类型标签`（txt2img→text2img、img2img 等）+ 模板自带标签，可用于与渠道关键词匹配。参考图和缩略图直接使用远程 URL；如需本地化，可在预设编辑中手动改为上传的图片。

## 保存目录

`save_dir` 留空时，生成的图片/视频默认保存在**插件目录**下（文件名形如 `20260812_103000_gemini_0.png`）。设置后保存到指定目录（例如 `R:\AstrBot\data\media-luna-images`）。

## 数据文件

插件目录下：

- `channels.json`：渠道定义（`connectorId` + 每渠道 `connectorConfig` + `tags`）
- `presets.json`：预设（触发词=键名，含 `promptTemplate`、`tags`、`referenceImages`、`thumbnail`、`source`）
- `tasks.json` / `stats.json`：任务记录与用量统计
- `media/`：WebUI 上传的缩略图/参考图

## 注意

- v1 渠道配置（`type/model/size` 格式）会在首次加载时自动迁移为 v2（`connectorId/connectorConfig`）。
- API Key 改为按渠道配置，请到 WebUI 各渠道里填写（或直接编辑 `channels.json`）。
- Midjourney 适配通用 Proxy（`/imagine` + `/result`），不同服务商的路径/鉴权头可能不同，按需调整 `apiUrl`。
- ComfyUI 需要先在渠道里配置工作流 JSON（API 格式），用 `{{prompt}}` 作为提示词占位符。
