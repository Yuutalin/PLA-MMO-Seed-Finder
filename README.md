# 阿尔宙斯 MMO Seed 反推助手 \| PLA MMO Seed Finder

面向 [Pokeldn](https://github.com/Decryptu/pokeldn) 的《宝可梦传说 阿尔宙斯》MMO Seed 反推插件。插件收集同一群体中四只**最初捕获的宝可梦**的交换确认页记录，并调用外部计算程序 **[EtumrepMMO](https://github.com/kwsch/EtumrepMMO)** 反推群体 Seed。计算结果会显示在 Pokeldn 界面，并保存为文本文件。

> 当前仓库对应插件版本：`v0.3.13`。自动化测试通过不等于已完成所有 Pokeldn、无线适配器和 Switch 实机环境验证。

## 项目简介

- **项目名称：** 阿尔宙斯 MMO Seed 反推助手
- **GitHub 仓库名建议：** `PLA-MMO-Seed-Finder`
- **插件 ID：** `junli-pla-mmo-seed`
- **当前版本：** `0.3.13`
- **运行平台：** Windows 桌面版 Pokeldn
- **核心用途：** 采集四只宝可梦的合法记录，并调用 EtumrepMMO 计算《宝可梦传说 阿尔宙斯》的 MMO 群体 Seed。

## 界面截图

请将你自己的截图放进 `docs/images/` 文件夹，并使用下列文件名替换。截图暂缺时，GitHub 页面会显示图片占位链接。

<!-- 截图 1：插件主界面、采集进度 -->

![插件主界面与采集进度](docs/images/01-main-interface.png)

<!-- 截图 2：四只宝可梦紧凑信息卡 -->

![四只宝可梦信息卡](docs/images/02-pokemon-cards.png)

<!-- 截图 3：Seed 计算成功与结果展示 -->

![Seed 计算结果](docs/images/03-seed-result.png)

<!-- 截图 4：配置或日志输出，可选 -->

![配置与运行日志](docs/images/04-settings-and-log.png)

## 主要功能

- 通过 Pokeldn 的本地交换流程采集宝可梦记录。
- 只有进入“确定要进行宝可梦的交换吗？”确认页面，才将当前宝可梦计入收录；单纯移动光标、停留或普通预览不会计数。
- 不会自动确认真正的交换。第四只记录保存后，插件会正常结束联机，再启动 Seed 计算。
- 以紧凑卡片显示宝可梦中文名称、等级、性别、性格、精灵球、个体值、表 ID / 里 ID、中文特性、官方 XS/S/M/L/XL 体型和招式。
- 优先读取 Pokeldn 本地宝可梦图标缓存；缺失时尝试后台下载，仍不可用则显示占位图。
- 最近保留 12 只宝可梦记录，便于检查最近的采集数据。
- 计算成功后在界面显示 Seed，并写入 `latest_seed.txt`；同时生成最近宝可梦信息的 TXT/JSON 摘要。
- 图标处理不依赖 Pillow。

## 工作流程

1.  在《宝可梦传说 阿尔宙斯》中前往祝庆村的交换 NPC，选择本地交换。
2.  在 Switch 与 Pokeldn 插件中输入相同的 8 位联机密码，等待连接成功。
3.  在宝可梦箱子中选中一只**最初捕获的宝可梦**，进入“确定要进行宝可梦的交换吗？”确认页面。
4.  等插件显示该只宝可梦已收录后，按 B 返回箱子。不要确认真正交换。
5.  选择同一 MMO 群体中的下一只最初捕获宝可梦，重复以上步骤，直到显示 `4/4`。
6.  第四只记录保存后，插件自动结束联机并启动 EtumrepMMO。
7.  如果成功找到群体 Seed，结果会显示在插件界面并保存到 `latest_seed.txt`。

**注意：** 计算需要来自同一 MMO 群体的四只有效记录。若未找到 Seed，请先检查宝可梦是否属于同一群体，以及记录是否均为最初捕获记录。

## 运行环境与依赖

### 必需环境

- Windows 桌面系统。
- 已安装并能正常使用插件功能的 Pokeldn 桌面版。
- 《宝可梦传说 阿尔宙斯》游戏、Nintendo Switch 和 Pokeldn 支持的本地联机硬件/无线适配器。
- Pokeldn 中配置正确且可访问的 Switch 密钥文件（`prod.keys`）。
- EtumrepMMO 官方 Windows x64 发布包。
- **.NET Runtime 8.0** 与 **Microsoft Visual C++ Redistributable**。如果计算程序无法启动，请检查这两项运行环境。

### 外部 Seed 计算程序

本插件负责采集、整理记录、界面展示和启动计算程序；**Seed 反推算法由独立的 EtumrepMMO 程序执行**。EtumrepMMO 不作为本仓库源码的一部分，请从其官方发布页获取：

- [EtumrepMMO 官方 Releases](https://github.com/kwsch/EtumrepMMO/releases/latest)

下载官方 Windows x64 发布包后，需将**压缩包内的全部文件**解压到以下目录的 `solver` 子文件夹中，不能只复制 `.exe`：

``` text
%APPDATA%\pokeldn\plugin-data\junli-pla-mmo-seed\solver\
```

在大多数 Windows 用户环境中，该路径对应：

``` text
C:\Users\<你的Windows用户名>\AppData\Roaming\pokeldn\plugin-data\junli-pla-mmo-seed\solver\
```

如果目录不存在，可以先运行一次插件，再手动创建 `solver` 文件夹。完成后重新运行“计算最近记录”或重新进行采集；已保存的记录通常无需重新采集。

## 插件文件说明

安装包 `.pokeplugin` 本质上是包含插件文件的压缩包。插件的核心文件应放在同一个插件目录中：

| 文件                      | 作用                                                              | 是否必需   |
|---------------------------|-------------------------------------------------------------------|------------|
| `plugin.toml`             | 插件元数据：ID、名称、版本、入口模块和描述                        | 是         |
| `plugin.py`               | Pokeldn 插件注册入口，注册工具和联机参数                          | 是         |
| `pla_mmo_seed.py`         | 主要实现：收录逻辑、记录保存、卡片界面、图标缓存、调用 EtumrepMMO | 是         |
| `pokemon-placeholder.png` | 缺少宝可梦图标时使用的占位图                                      | 是         |
| `test_plugin.py`          | 本地自动化测试，供开发和修改后回归检查                            | 开发时需要 |
| `README.md`               | 项目介绍、安装方式和使用说明                                      | 建议保留   |
| `CHANGELOG.md`            | 版本更新记录                                                      | 建议保留   |
| `docs/`                   | 检查报告、测试结果和项目截图                                      | 可选       |

正常用户优先安装发布页提供的 `.pokeplugin` 文件；只有进行开发、调试或自行打包时，才需要直接操作源码文件。

## 安装与配置

### 普通用户

1.  从 GitHub Releases 下载最新的 `.pokeplugin` 安装包。
2.  使用当前 Pokeldn 版本提供的插件安装入口导入插件。
3.  确认 Pokeldn 的 Switch 密钥路径可用，并准备支持本地联机的硬件环境。
4.  按上一节说明安装 EtumrepMMO 及其运行依赖。
5.  启动插件，按工作流程收录四只宝可梦。

### 开发者 / 手动安装

1.  克隆本仓库。
2.  确认 `plugin.toml`、`plugin.py`、`pla_mmo_seed.py` 和 `pokemon-placeholder.png` 位于同一插件目录。
3.  将插件目录按 Pokeldn 当前版本支持的方式加载或打包为 `.pokeplugin`。
4.  安装外部 EtumrepMMO 并配置 `solver` 目录。
5.  先运行自动化测试，再在独立测试环境中验证 Pokeldn 和 Switch 的真实联机流程。

## 数据保存位置

插件使用 Pokeldn 的插件数据目录。默认 ID 对应的路径为：

``` text
%APPDATA%\pokeldn\plugin-data\junli-pla-mmo-seed\
```

常见内容如下：

| 路径                          | 内容                                           |
|-------------------------------|------------------------------------------------|
| `latest_seed.txt`             | 最近一次成功计算得到的群体 Seed                |
| `latest_pokemon_details.txt`  | 最近一次采集的宝可梦信息文本摘要               |
| `latest_pokemon_details.json` | 最近一次采集的宝可梦信息 JSON 摘要             |
| `runs/`                       | 采集会话与宝可梦记录；程序按保留策略清理旧记录 |
| `sprite-cache/`               | 下载的宝可梦图标缓存                           |
| `solver/`                     | EtumrepMMO 官方发布文件及其计算输入目录        |

插件会限制历史记录与图标缓存的增长，但这些属于本地运行数据，不应提交到公开 GitHub 仓库。

## 实现方式

1.  **Pokeldn 插件注册：** `plugin.py` 通过 `pokeldn.plugins` 提供的 `Field` 和 `Tool` 注册入口、联机密码、无线信道和最长等待时间等参数。
2.  **联机与记录采集：** `pla_mmo_seed.py` 调用 Pokeldn 提供的阿尔宙斯本地交换功能，并监测对应的交换确认页面事件；只在确认页面对应的记录事件出现后保存宝可梦数据。
3.  **数据持久化：** 保存四只宝可梦的 PA8 记录及界面需要的信息，维护最近记录窗口，并生成文本/JSON 摘要。
4.  **原生界面更新：** 使用 Pokeldn 的插件面板接口展示进度、紧凑宝可梦卡片和 Seed 结果。图标优先从本地缓存读取，缺失时通过标准库网络接口尝试下载；图像格式检查不依赖 Pillow。
5.  **Seed 计算：** 使用 Python `subprocess` 启动 `solver` 目录中的 EtumrepMMO 可执行文件，读取其输出并判断是否获得最终群体 Seed。
6.  **结果保存：** 成功时更新界面、写入 `latest_seed.txt`，并尝试打开结果文本文件。

## 源码检查

当前 `v0.3.13` 源码包的本地检查结果：

- Python 语法编译检查通过。
- 自动化测试：31 项通过。
- 两列布局对齐断言通过。
- ZIP 包完整性检查通过。
- **未完成用户电脑上的 Pokeldn + Switch 实机验证**；实际环境仍需安装后确认。

运行测试：

``` bash
python -m unittest -v test_plugin.py
```

## 安全与使用说明

- 不要把 `prod.keys`、个人存档、真实采集到的 `.pa8` 文件、`runs/`、`latest_seed.txt` 或个人运行日志提交到公开仓库。
- `solver/` 中的 EtumrepMMO 文件应从其官方发布页单独下载；本仓库不重新打包或修改该程序。
- 请尊重 Pokeldn、EtumrepMMO、游戏及相关图像资产各自的许可与使用条款。
- 本项目为社区工具，与 Nintendo、The Pokémon Company、GAME FREAK 或 Pokeldn 官方不存在隶属或背书关系。

## 许可证

当前版本尚未在仓库中指定开源许可证。公开发布前，请确认你有权公开分发所提交的源码和资源，并根据项目的实际授权选择并添加 `LICENSE` 文件。
