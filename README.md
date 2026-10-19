# AuthSwitch

Windows 终端里的官方订阅账号认证切换工具。支持 **Codex / ChatGPT 订阅**和 **Claude Code 官方订阅**，管理账号、手动切换认证、查看额度与重置时间。Codex 还支持从 CC Switch 导入订阅账号。

同一时间使用一个账号。所有账号使用同一个 Codex 配置目录，工具仅替换 `auth.json`，不替换 `config.toml`、对话记录、项目、MCP 或技能。切换后重新启动 Codex，执行 `codex resume` 选择以前的对话；跨账号续接的实际服务行为仍需实机确认。

## 运行

需要 Windows、Python **3.11 或更新版本**，以及要使用的 Codex CLI 或 Claude Code 原生 Windows 可执行文件。无需额外 Python 依赖。

在本项目目录运行：

```powershell
# 打开交互菜单
.\auth-switch.ps1

# 只读检查环境
.\auth-switch.ps1 doctor

# 或直接使用 Python
python -m authswitch --help
```

也可以双击 `auth-switch.cmd` 打开菜单。如果 PowerShell 本地策略禁止执行 `.ps1`，直接运行 `auth-switch.cmd` 或 `python -m authswitch`，不需要修改系统执行策略。

从其他项目目录调用时，使用启动脚本的完整路径；脚本不会把当前工作目录切到工具项目。

### 简短命令

推荐一次安装，让新终端默认可用（只需执行一次）：

```powershell
.\install-auth.ps1
```

安装器把项目的 `bin` 目录加入**当前用户 PATH**，使 CMD / PowerShell 在任意目录找到 `auth`；并为 PowerShell 7、Windows PowerShell 5.1 的当前用户启动文件追加一个带标记的加载块，让新窗口自动支持别名 Tab 补全。不需要管理员权限，不更改执行策略。原启动文件有改动前会创建备份，重复安装不会重复追加 PATH 或加载块。

仅预览修改位置、不安装：`.\install-auth.ps1 -Preview`。安装后保留本项目路径；若 CMD 新窗口仍未获得 PATH，退出整个终端应用后重新打开。使用 `-NoProfile` 启动的 PowerShell 不加载补全，仍可通过 PATH 执行 `auth`。

只想临时加载时：

在 PowerShell 中加载一次，即可在当前窗口的任意目录使用 `auth`：

```powershell
. .\enable-auth.ps1

auth                  # 打开主菜单
auth list             # 显示编号，输入编号即可切换
auth use 2            # 直接切换列表中的第 2 个账号
auth use m            # 唯一前缀匹配 main；多个匹配时提示，不切换
auth use ma<Tab>      # 按 Tab 补全账号别名（不要输入字面量 <Tab>）
auth usage --all      # 查询所有账号的额度和重置时间
auth order main backup # 固定列表顺序：main 为 1，backup 为 2
```

快捷命令只作用于当前 PowerShell 窗口，不修改 PATH 或 PowerShell 配置文件。新窗口重新加载即可；在其他目录加载时，使用 `enable-auth.ps1` 的完整路径。若已有同名的 `auth` 命令，脚本会停止，不覆盖它。

账号选择可使用编号、名称、唯一名称前缀（不区分大小写）或 ID；如果账号名称本身是数字，优先匹配这个名称。多个前缀匹配时列出候选，不执行切换。加载脚本同时注册 Tab 补全；补全只读取本地别名，不查询用量。

## 服务名放最后

格式：`auth <操作> [账号] [选项] [codex|claude]`。省略服务名时仍然默认 Codex，已有短命令继续可用。

```powershell
auth list codex
auth list claude
auth use main codex
auth use main claude
auth use m claude
auth usage --all claude

auth --help                  # 总帮助，合并说明 Codex 和 Claude
auth list --help             # 两个服务的列表用法
auth use --help              # 两个服务的切换用法，不切换账号
```

`codex` 和 `claude` 是保留服务标识，不能用作新账号别名。编号、别名、前缀都只匹配所选服务的账号；两个服务可以各自有一个 `main`。帮助无需区分服务，会统一给出两种服务的示例，并标明专属功能。兼容 `auth --help claude` 等旧写法，但仍显示相同的合并帮助。

Tab 补全读取末尾服务名；例如在 `auth use m claude` 中把光标放到 `m` 后按 Tab，只补全 Claude 的别名。服务名也支持补全，例如 `auth use main cl` 后按 Tab。末尾尚未写服务名时默认补全 Codex 账号。已打开的 PowerShell 可重新加载 `enable-auth.ps1` 更新补全，或打开一个已安装自动加载的新窗口。

### 调整两台电脑的账号顺序

列表编号按本机保存的顺序排列，默认会受添加、导入先后影响。可以指定并持久保存顺序：

```powershell
auth order main backup          # Codex：main 排第 1，backup 排第 2
auth order main backup claude   # Claude 同样支持
auth order 2 1                  # 按当前列表编号交换前两个账号
```

所有编号都按操作前的同一份列表解析。未指定的账号保持相对顺序，排在指定账号后面；重复账号或无效选择会中止，不修改顺序。调整仅保存本机列表，不切换登录，也不需要退出正在使用的 Codex / Claude。主菜单也可选择“9 调整顺序”。

两台电脑分别执行相同的**别名顺序**即可对齐编号；顺序不会自动跨设备同步。跨设备日常切换也可直接使用 `auth use main`，不依赖编号。

## Claude Code

首次使用时，可以在当前 Claude 会话运行期间只备份当前账号：

```powershell
auth doctor claude           # 只读检查路径和运行中的客户端
auth save main claude        # 保存当前账号；不改 Claude 的登录文件
auth list claude             # 查询一次用量，按编号切换
auth list --cached claude    # 只读缓存，不发起用量请求
```

添加第二个账号或切换时，先正常退出 Claude Code（含 IDE 中的会话），再运行：

```powershell
auth login backup claude     # 隔离目录内打开官方订阅登录
auth use backup claude       # 明确应用到共用配置目录
claude --resume              # 选择原来的本地对话
```

- Claude 账号保存在 `%LOCALAPPDATA%\AuthSwitch\claude\accounts.dat`。原有 Codex 账号库的位置和内容不因新增服务而改变。
- 共享配置目录默认是 `%USERPROFILE%\.claude`，或 `CLAUDE_CONFIG_DIR`。默认账号身份文件是 `%USERPROFILE%\.claude.json`；显式设置 `CLAUDE_CONFIG_DIR` / `--claude-home` 时使用该目录内的 `.claude.json`，也可通过 `--claude-config` 明确指定。
- 切换只替换 `.credentials.json` 的 `claudeAiOauth` 和 `.claude.json` 的 `oauthAccount`，保留其他登录凭据、项目设置、聊天记录和共享的本机标识。不复制整个配置目录。
- 切换前保存当前账号最新认证，并加密备份两个原文件。两次原子写入之间出错时尝试回滚；发现外部修改会中止，不用旧备份覆盖并发变化。回滚无法完成时明确报告，备份留在加密库内。
- 检测到 Claude Code 或 CC Switch 运行时，阻止 `use`、`login` 和 `refresh`。不会自动杀进程。Codex 进程不会单独阻止 Claude 操作，反之亦然。
- `login` 使用子进程专属的配置目录、用户目录和应用数据目录，明确执行官方 `claude auth login --claudeai`。不执行 logout，不把新认证自动写入共用目录。
- Claude 的 `refresh` 是重新执行官方浏览器登录，然后通过 `use` 应用；不自行实现私有令牌刷新请求，也不提供 Codex 的设备码登录参数。

Claude 用量读取使用客户端内可见的 OAuth 只读端点 `https://api.anthropic.com/api/oauth/usage`，它不是稳定的公开 API，可能因版本或服务端变更失效。查询仅发送现有访问令牌，不刷新令牌，不写 Claude 配置，不创建模型会话；禁止跟随 HTTP 重定向。访问令牌过期或服务返回 401/403/429 时显示失败并保留旧缓存；可以让正在运行的原生会话更新令牌后重新 `save`，或退出后 `login` 重新授权。`list` 自动查询只执行一次，不后台轮询。

可显示 5 小时、每周及服务返回的模型专项周额度，沿用简洁的一行格式和百分比颜色。非额度字段不自行折算成剩余 Token。跨账号续接本地对话仍需真实验证，组织的云端资源和权限由登录账号决定。

自定义路径示例，服务名仍放最后：

```powershell
auth --claude-home D:\ClaudeHome --claude-config D:\ClaudeHome\.claude.json list --cached claude
```

## 从 CC Switch 迁移

1. 先预览，确认两个官方订阅账号可识别。这一步不写入文件：

   ```powershell
   .\auth-switch.ps1 import-ccswitch --dry-run
   ```

2. 退出 CC Switch，包括托盘进程。正式迁移前也退出 Codex CLI、Codex 桌面端和使用 Codex 的 IDE 会话。不要让两个工具交替刷新同一账号的凭据。

3. 保存当前登录，再导入已有账号：

   ```powershell
   .\auth-switch.ps1 save main
   .\auth-switch.ps1 import-ccswitch
   .\auth-switch.ps1 list
   ```

4. 导入账号默认使用邮箱作为名称，可使用 `list` 显示的 ID 重命名：

   ```powershell
   .\auth-switch.ps1 rename <账号ID> backup
   .\auth-switch.ps1 usage --all
   .\auth-switch.ps1 use backup
   codex resume
   ```

5. 两个账号的真实查询、切换和续接均验证成功后，再卸载 CC Switch。工具不会卸载软件，也不会改写 CC Switch 的原文件或数据库。

CC Switch 的订阅文件通常是 `%USERPROFILE%\.cc-switch\codex_oauth_auth.json`，并非数据库中的普通服务配置。可通过 `--source` 指定这个文件或它所在的目录。本机已进行只读格式检查，可识别两个账号。

**导入只复制认证快照。** CC Switch 可能仅保存刷新令牌和身份令牌，没有访问令牌；工具会标为“导入凭据待补全”。首次切换或查询时，工具尝试让隔离的 Codex 进程补全认证，不自行拼接 OAuth 刷新请求。如果刷新失败或旧令牌已失效，通过下面的官方登录更新该账号：

```powershell
.\auth-switch.ps1 login backup
# 浏览器选择该账号，官方授权完成后：
.\auth-switch.ps1 use backup
```

重复导入会跳过已保存账号，不用旧快照覆盖已经刷新的凭据。

## Codex 命令（默认服务）

| 命令 | 作用 |
| --- | --- |
| 不带命令 | 交互菜单 |
| `doctor` | 只读检查路径、存储方式、运行中的客户端 |
| `list` | 打开时查询一次全部账号的用量，显示编号；在终端输入编号切换，回车或 `0` 退出 |
| `list --cached` | 只看缓存，不查询用量；交互终端仍可选择切换 |
| `list --no-interactive` | 只查看列表，不提示切换 |
| `list --json` | 输出公开元数据，不输出认证令牌 |
| `save main` | 加密保存当前 Codex 登录，不修改当前认证 |
| `import-ccswitch --dry-run` | 只预览导入来源 |
| `import-ccswitch` | 导入 CC Switch 的 Codex 订阅账号 |
| `login backup` | 在隔离目录完成官方浏览器登录，保存后需 `use` 激活 |
| `login backup --device-auth` | 使用官方设备码登录，需账号支持 |
| `refresh backup` | 退出客户端后，让 Codex 刷新保存的凭据，之后需 `use` 激活 |
| `use backup` | 保存当前账号最新凭据，切换到目标账号 |
| `rename backup second` | 修改账号名称 |
| `order main backup` | 持久保存列表顺序和编号，不切换账号 |
| `usage` | 查询已保存的当前账号 |
| `usage backup` | 查询指定账号 |
| `usage --all` | 依次查询全部账号 |

`list` 的切换仍要求先退出 Codex 和 CC Switch。`list --json`、输入输出重定向或管道使用时只显示数据，不等待选择；仍默认刷新一次，查询进度输出到 stderr。脚本或离线读取可使用 `list --json --cached`。查询失败时继续显示列表和标记过期的缓存。主菜单中的“切换账号”也使用编号列表并刷新一次。

需要路径参数时，放在子命令之前：

```powershell
.\auth-switch.ps1 --codex-home D:\MyCodex --data-dir D:\PrivateAuthSwitch list
.\auth-switch.ps1 --codex-bin 'C:\path\to\codex.exe' usage --all
```

默认共享目录遵循 `CODEX_HOME`，未设置时为 `%USERPROFILE%\.codex`。默认凭据库为 `%LOCALAPPDATA%\AuthSwitch`，可通过 `AUTHSWITCH_HOME` 或 `--data-dir` 更改。两个目录必须彼此独立。

## 用量与 reset

“流量”显示为官方订阅额度，不用本地 Token 数自行推算。使用 Codex App Server 的 `account/rateLimits/read` 读取：

- 各额度窗口的长度、已用百分比与剩余百分比。
- 服务返回的重置时间，转换为本地时间；今天仅显示时分，其他日期显示月日和时分。
- 每个账号紧凑显示，例如 `5h 97% (17:20 reset) | 7d 15% (10/21 06:00 reset)`，百分比表示**剩余**额度。
- 只有百分比变色：剩余 ≥50% 绿色、20%–49% 黄色、<20% 红色；窗口、分隔符和 reset 时间保持白色。重定向和 `NO_COLOR` 环境下不输出颜色。
- 查询成功直接显示一行；读取缓存时在行尾显示简短时间，失败保留旧缓存并明确标记。

不把所有套餐写死成“5 小时＋每周”。没有返回的窗口或时间显示为 `?` / `--%`；重置时间已过时标记“待刷新”，不擅自显示额度恢复。

查询使用独立、短期的 Codex App Server，不创建模型会话，不修改当前 `auth.json`。已有访问令牌通过外部令牌模式查询，避免替正在运行的客户端刷新认证。导入账号需要补全凭据，或非当前账号令牌已过期需要刷新时，要求先退出客户端。当前账号访问令牌已过期时，先执行 `refresh 名称`，再 `use 名称`，随后重新启动 Codex。

查询子进程的 `CODEX_HOME` 是临时目录，不继承当前 Codex 的配置或环境认证覆盖。子进程结束后删除临时目录。不会自动切换到额度更多的账号，也不会兑换任何额度重置权益。

## 凭据与切换行为

- 保存的账号认证及用量缓存统一写入 `accounts.dat`，使用 Windows DPAPI 绑定当前 Windows 用户加密。不能把它当作跨设备的账号导出文件。
- 凭据库目录限制为当前用户和 SYSTEM；不提供明文存储降级。
- Codex 实际使用的 `auth.json` 仍采用 Codex 本身的存储格式。登录、刷新和导入补全期间，私有临时目录内也会短暂存在该格式的文件；正常退出会清理。进程被强行终止或断电时，临时文件可能残留在凭据库的 `jobs` 目录内。
- 切换前检查 Codex 与 CC Switch 进程；不自动杀进程，没有跳过检查的开关。
- 切换前保存最新的当前认证，并在加密库内记录上一次切换前的认证备份。若当前账号从未保存，自动以“原账号-…”名称保存。切回时直接使用该账号的名称或 ID。
- 对认证文件使用同目录原子替换。替换前再次检查进程和原文件；文件变化时中止。操作失败不会主动删除原认证文件。
- `login` / `refresh` 产生的新认证先保存在库内，需 `use` 应用。不会在同一账号切换时被旧的本机凭据覆盖。
- 多个工具实例通过文件锁避免同时写入。

Codex 仅支持**文件凭据存储**。若 `cli_auth_credentials_store` 为 `keyring`、`auto` 或 `ephemeral`，工具会停止对应操作，不自动修改你的配置。Claude 适配 Windows 文件凭据存储，不支持 macOS Keychain。组织强制认证策略和自定义认证网关尚未适配。

## 验证

```powershell
python -m unittest discover -s tests -v
```

测试使用虚构账号，覆盖切换保存刷新后的凭据、保留对话和配置、异常时不覆盖认证、重复导入、失败查询保留缓存、DPAPI 加密以及输出不包含令牌。测试夹具保留沙箱继承权限，生产目录的 ACL 收紧需要在普通 Windows 用户会话中确认。

新增 Claude 模拟测试覆盖双文件切换及回滚、保留项目与其他 OAuth 数据、运行中阻止切换、隔离官方登录、只读用量查询、服务独立账号库、末尾服务参数与帮助、按服务区分的 Tab 补全。排序测试验证持久顺序、按原编号解析、非法选择不修改列表，以及保持认证和另一个服务的数据不变。当前共 61 项测试。

本机已完成 Codex CLI 0.163.0 的隔离协议握手与无登录状态读取、CC Switch 两账号文件的只读兼容检查。**未替换真实账号认证，未刷新真实令牌，未验证线上用量返回和跨账号续接。** 首次真实验证请在退出客户端后进行。

Claude 已完成隔离目录的 `auth login --help` / 未登录 `auth status` 检查，确认自定义配置目录的身份文件位置；`doctor claude` 只读识别了本机订阅认证和正在运行的 Claude 进程。开发测试未登录真实账号、未切换真实认证、未发送真实用量查询、未停止原 Claude 进程。

参考：[Codex 认证与凭据存储](https://learn.chatgpt.com/docs/auth)、[Codex App Server](https://learn.chatgpt.com/docs/app-server)、[Claude 认证与凭据存储](https://code.claude.com/docs/en/authentication)、[Claude CLI 命令](https://code.claude.com/docs/en/cli-reference)。
