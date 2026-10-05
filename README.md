# OpenTale Demo

一个可以离线验证的共同生成叙事实验：多个叙述位置共同使用表达，各自保留不同的归属与作用；一处的变化实际改变别处下一次生成的条件，呈现位置再从这些关系中形成一条可读序列。

主仓库仅为本目录。系统用确定性的运行内核管理关系与续延，用可替换的模型生成内容。默认 `fixture` 完全离线，不需要 API key。离线样例验证机制，不能证明真实模型的语义耦合、审美或文学效果。

## 启动

需要 [uv](https://docs.astral.sh/uv/) 和 Node.js 20+（含 npm）；源码要求 Python 3.11+，项目固定使用 Python 3.13。启动脚本缺少该版本时允许 uv 将解释器下载到本仓库 `.toolchain/python`，依赖与缓存也保留在本仓库。命令从仓库根目录执行：

```sh
python3 scripts/bootstrap.py
uv run python scripts/dev.py build
uv run python scripts/dev.py serve
```

打开 <http://127.0.0.1:8231>。生产网页与 API 由同一个后端进程提供，只监听本机。端口被占用时可使用 `serve --port 8232`。

独立于当前终端运行：

```sh
uv run python scripts/dev.py detach
uv run python scripts/dev.py status
uv run python scripts/dev.py stop
```

`detach` 自动在缺少产物时构建网页，在 `runtime/` 保存私有进程记录和启动日志；`stop` 检查进程身份后再停止，保留所有存档。运行中的本机程序不因关闭 Codex 而退出；操作系统重启后需重新启动。

受限沙箱可能不允许监听回环端口或检查进程身份。脚本会明确报告权限限制；无法验证身份时保留记录并拒绝停止，请在有相应权限的本地终端执行。

生产服务不自动重载源码。更新代码后先重新构建，再 `stop` / `detach`；只有配置参数使用网页重新加载。

前后端分别开发时，在两个终端执行：

```sh
uv run python -m uvicorn opentale.api:app --host 127.0.0.1 --port 8231 --reload
```

```sh
cd frontend
npm run dev
```

开发网页使用 <http://127.0.0.1:5196>，代理 `/api` 至后端 8231。运行时快照、事件与进程记录均在忽略的 `runtime/` 中；删除指定对局会删除该对局的数据库记录，网页会要求明确确认。当前没有自动备份；导出副本由用户管理。

## 第一轮离线实验

网页创建 `letters` 预设，选择 **离线 fixture**，先运行一个窗口。随后查看：

1. 正文阅读区的一段呈现，与人物位置各自形成的内容。
2. 关系及诊断区的共享表达、带作用的使用关系、多来源共同目标。
3. 反事实消融：移去一个来源，观察条件指纹和 fixture 续写是否改变。原对局不被改写。
4. 新建分支继续、暂停、导出及删除。

公开正文和工程诊断是不同投影。模型获得的角色视图不含“哪个计算位置创造了我”等技术来源信息；读者正文也不显示内部操作地址。

## 可重复验收

```sh
uv run pytest
uv run ruff check backend tests scripts
cd frontend
npm test -- --run
npm run build
cd ..
uv run python scripts/verify_demo.py
uv run python scripts/check_secrets.py
```

HTTP 验收脚本另起随机本地端口和隔离临时存储，检查实际生命周期与服务重启，不读取本仓库 `.env.local`，不会请求付费 API。密钥检查扫描公开源码与 Git 暂存区，发现私有路径时仅报告路径，不读取私有文件。它不是对所有潜在秘密的完整证明。

## 启用真实模型

**将密钥只填写到本机文件 `/Users/huchenzi/Ready4AI/Project_OpenTale/Opentale_Demo/.env.local`。不要填写到 Markdown 文档、网页、对话或 Git 中。** 仓库已准备忽略的空文件；公开的 `.env.example` 只列字段名和非敏感默认值。

用本地编辑器填写：

```dotenv
OPENTALE_API_KEY=<在本地编辑器填写>
OPENTALE_BASE_URL=https://api.deepseek.com
OPENTALE_MODEL=deepseek-flash
OPENTALE_TIMEOUT_SECONDS=90
OPENTALE_MAX_OUTPUT_TOKENS=1600
OPENTALE_PORT=8231
```

设置文件权限：

```sh
chmod 600 .env.local
```

不要用会进入命令历史的带明文密钥命令。配置修改后，在没有任务运行时点击网页的“重新加载配置”，再创建 `deepseek` 对局。网页只显示配置是否就绪、模型名称和安全参数；密钥及服务地址不发往浏览器。

先用很小的调用、输出 token 和窗口预算单步测试。重试和失败尝试仍可能计费，预算是准入及停止控制，不是服务商账单的严格保证。当前实现不需要公开 CoT，也不会保留或显示模型推理内容。开发验收默认始终使用 fixture；提供密钥不等同于允许无上限测试。

## 设计与边界

- [架构与执行逻辑](docs/ARCHITECTURE.md)：E/U/K、联合更新、恢复、序列化及当前简化。
- [验收矩阵与验证范围](docs/VALIDATION.md)：哪些证据证明机制，哪些尚须真实模型实验。
- [私有配置与安全约束](docs/SECURITY.md)：密钥、错误、导出、存档与本机边界。
- [模块和 HTTP 契约](docs/CONTRACT.md)：可执行接口与模块责任。
- [变更记录](CHANGELOG.md)。

v0.1 没有训练新模型、没有神经隐状态共享、没有动态位置生成或跨分支合并，也没有声称某种公共历史才是故事真相。它实现的是程序层面的真实依赖，真实模型能否充分使用这种依赖，是下一阶段需要检验的问题。
