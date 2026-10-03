# AI Camera Agent V2

AI + IoT 黑客松摄影机器人项目的 **Agent 软件部分**。用户描述想拍什么，Planner 生成随时间变化的画面目标；Feedback 比较计划与实测，输出受约束的继续、修正或暂停决策。

**当前状态：DEMO READY；尚未 REAL ROBOT VALIDATED。** 核心处于冻结后的 INTEGRATION MODE，后续只根据团队正式接口增量替换 Adapter。

| 模块 | 状态 | 说明 |
| --- | --- | --- |
| Planner V2 / Visual Contract | PASS | ShotScript `0.2`、FrameState、分段线性 TargetTrajectory |
| Feedback V2 | PASS | 比较位置、主体高度占比及可选距离 |
| Planner V2 Real DeepSeek | PASS | `deepseek-flash`，已完成一次真实 Structured Output smoke |
| Mock Compiler / Offline E2E | PASS | 只验证接口与软件链路，不推导机械运动 |
| 离线测试 | 507 passed | 10 项 integration 默认 deselected；无需网络或 API Key |
| 真实 Observation / Reachability / Compiler / Executor | WAITING | 等待团队 Contract |

## 1. 拉取和安装

公开仓库可以直接拉取；提交 PR 不需要先取得仓库写权限，可以采用 Fork 流程。

```powershell
git clone https://github.com/hard-66/ai-camera-agent-v2.git
cd ai-camera-agent-v2
python -m venv .venv
.venv/Scripts/python.exe -m pip install -e ".[test]"
```

要求 **Python 3.11+**。无需安装 App、Vision、ROS 或任何硬件驱动。
Linux/macOS 可使用 `python3 -m venv .venv`，后续将 `.venv/Scripts/python.exe` 换成 `.venv/bin/python`。

## 2. 运行 Demo

```powershell
.venv/Scripts/python.exe -m agent_system.demo_v2
```

默认固定拍摄对象为桌面青铜器，展示：

- 0 秒：主体偏低，高度占画面 40%。
- 2.5 秒：目标 center_y=0.60，高度比例=0.55。
- 5 秒：主体居中，高度占画面 70%。
- 正常画面 → CONTINUE；位置/大小偏差 → ADJUST + 画面修正 components；主体丢失 → PAUSE。
- UNREACHABLE 在执行之前拒绝；另有距离目标存在但测量缺失的 PAUSE 案例。

`low=0.70`、`middle=0.50` 是 **MOCK / DEMO TARGET VALUE**，不是硬件标定标准；主 Demo 不补造距离。

```powershell
.venv/Scripts/python.exe -m agent_system.demo_v2 --scenario normal
.venv/Scripts/python.exe -m agent_system.demo_v2 --scenario offset
.venv/Scripts/python.exe -m agent_system.demo_v2 --scenario lost
.venv/Scripts/python.exe -m agent_system.demo_v2 --scenario unreachable
.venv/Scripts/python.exe -m agent_system.demo_v2 --scenario distance-missing
```

## 3. 软件链路与 Mock 边界

```text
UserRequest → Planner V2 → ShotScript 0.2 / TargetTrajectory
                              ↓
                       Reachability Adapter
                              ↓
                       Motion Compiler Adapter
                              ↓
                  ShotExecutionPlan → 原子 Validator
                              ↓
                         Executor Adapter
                              ↓
External Payload → Observation Adapter → Observation V1 → Gate
                                                          ↓
                evaluate(explicit trajectory_time) → Feedback V2
                                                          ↓
                           CONTINUE / ADJUST / PAUSE
```

真实软件：Pydantic Contract、轨迹插值、Planner Pipeline、Registry/Validator、状态机、Feedback V2，以及已验证的 DeepSeek Adapter。

默认模拟：FakeDirectorLLM 是预设 fixture，并不实际理解任意自然语言；Reachability、Motion Compiler、Executor、Observation 和修正权限均为 **MOCK ONLY**。
Mock Compiler 只接受注册的 Demo fixture，生成 `mock_record_fixture` 记录动作，不计算升降、横梁、底盘、云台或电机参数。
ADJUST 停在视觉域的 TrajectoryCorrectionIntent；PAUSE 是请求，不代表硬件已经确认暂停。

V0 Director、Tool Mapper、静态 Feedback 和相关测试继续保留。当前不新增 Agent，不重构核心，不把真实机械差异放入 Planner/Feedback。

## 4. 测试

```powershell
.venv/Scripts/python.exe -m pytest -q -W error
```

当前验证结果：**507 passed / 10 deselected，exit 0**。默认测试不访问真实模型、不需要 `.env`、App 或硬件。

可运行交付链路的相关测试：

```powershell
.venv/Scripts/python.exe -m pytest tests/test_motion_compiler.py tests/test_planner_v2.py tests/test_demo_v2.py -q -W error
```

## 5. 可选：真实 DeepSeek Planner smoke

先安装现有可选依赖：

```powershell
.venv/Scripts/python.exe -m pip install -e ".[test,openai]"
Copy-Item .env.example .env
```

仅在本机配置 `.env` 中的 `OPENAI_API_KEY`、`OPENAI_BASE_URL`、`AGENT_OPENAI_MODEL`。
`.env` 被 Git 忽略；不得把凭据提交到源码、PR、日志或聊天中。

显式开启一次真实测试：

```powershell
$env:RUN_AGENT_PLANNER_V2_SMOKE='1'
$env:PYTHONUTF8='1'
.venv/Scripts/python.exe -m pytest tests/test_planner_v2_smoke.py -m integration -q -W error -s
```

真实调用会产生 Provider 成本。已有结果见 [Planner V2 真实验证记录](real_planner_v2_validation.md)；V0 历史记录见 [real_llm_validation.md](real_llm_validation.md)。
真实 Planner PASS 不代表 Reachability 或 Robot Motion PASS。

## 6. 团队接口接入

正式信息记录到 [integration_intake.json](integration_intake.json)，接入规则见 [INTEGRATION_INTAKE.md](INTEGRATION_INTAKE.md)。

**手机入口：** ShotPilot / 镜导 Android。协议见 [docs/APP_INTEGRATION.md](docs/APP_INTEGRATION.md)。App 默认 `fixture + dry_run`，上报像素框（`space=pixel`）。契约测试：`tests/test_shotpilot_contract.py`。

| 分类 | 团队需要提供 | Agent 侧接入位置 |
| --- | --- | --- |
| APP_VISION | payload 样例；bbox 坐标/画幅约定；距离单位/缺失表达；timestamp 时钟；Shot/Plan 关联 | Observation Adapter → 内部 Observation V1 |
| REACHABILITY | 校验入口；所需场景/初始状态/标定；能力版本；三种 verdict 与 reason | ReachabilityValidator Protocol |
| MOTION_COMPILER | 真实映射入口；Action/Function 参数 Schema、单位、范围、约束、revision；修正能力 | MotionCompiler Protocol → 现有 Plan / Validator |
| EXECUTOR | 提交及执行事件样例；ID 关联；暂停确认；执行时间关联 | Executor Adapter → ExecutionEvent / AgentState |

状态仅使用 WAITING / PARTIAL / READY_FOR_ADAPTER / INTEGRATED / VALIDATED。
每批信息采用 **失败 Contract Test → 最小 Adapter → 相关测试 → 全量回归**；通过后 INTEGRATED，真实层验证后才 VALIDATED。
联调顺序：Real Observation + Mock Compiler → Real Compiler + Mock Observation → 两者真实 → 完整真实系统。
距离内部单位与 pause/resume 时间策略仍待团队确认；Feedback 只消费明确的 `trajectory_time`。

## 7. 队友如何提交 Pull Request

没有写权限时，先在 GitHub 页面点击 **Fork**，再拉取自己的 Fork：

```powershell
git clone https://github.com/YOUR_GITHUB_NAME/ai-camera-agent-v2.git
cd ai-camera-agent-v2
git remote add upstream https://github.com/hard-66/ai-camera-agent-v2.git
git fetch upstream
git switch -c integration/app-vision upstream/main
```

已有写权限则可直接 clone 主仓库，然后 `git switch -c integration/app-vision`。
每个 PR 只处理一个明确接口批次；修改已有文件前保留本地备份。

```powershell
# 按前面步骤安装依赖；先编写失败 Contract Test，再做最小 Adapter 修改
.venv/Scripts/python.exe -m pytest -q -W error
git status
git add path/to/changed_adapter.py tests/path/to/contract_test.py integration_intake.json
git commit -m "Integrate confirmed App/Vision observation contract"
git push -u origin integration/app-vision
```

在 GitHub 打开 **Compare & pull request**，base 选择 `hard-66/ai-camera-agent-v2:main`。
填写仓库自带的 [PR 模板](.github/pull_request_template.md)：接口来源、确认值、缺失信息、测试结果和真实验证范围。
需要同步进度时更新 `agent_progress.json` 和 `AGENT_PROGRESS.md`；未经真实验证，不将状态提前标记 VALIDATED。
上述 `git add` 路径为示例，替换为本次实际修改文件；不要提交 `.env`、虚拟环境或本地备份。

## 8. 文件导航与交付材料

- [agent_system/](agent_system/)：核心 Contract、Planner、Feedback、Adapter 边界和 Demo。
- [tests/](tests/)：离线测试与显式隔离的真实 integration smoke。
- [AGENT_PROGRESS.md](AGENT_PROGRESS.md)：开发与集成状态。
- [integration_baseline.json](integration_baseline.json)：冻结时的源码/测试指纹和历史证据；不是运行时依赖。
- [docs/DEVELOPMENT_HISTORY.md](docs/DEVELOPMENT_HISTORY.md)：原 README 的完整开发历史，保留 V0/各阶段记录。
- [交付/](交付/)：已完成的比赛 Word 文档及配套素材；素材来源记录随文件保留。

上传包含项目源码、测试、配置样例、接口记录、验证报告、进度文件和已交付材料。
本机 `.env`、虚拟环境、缓存、备份、下载参考和 `_工作文件` 草稿/预览不进入仓库；它们仍保留在本地。
历史冻结清单记录的是 README 改写前的指纹；原文已保存至 docs/DEVELOPMENT_HISTORY.md，核心源码和测试保持原样。

## 9. 打包下载

除正常 Git clone 外，可下载 [Agent V2 完整交付 ZIP](artifacts/ai-camera-agent-v2-demo-ready.zip)。
ZIP 包含源码、测试、配置样例、接口与验证文档、进度文件和最终比赛材料；不包含凭据、虚拟环境、缓存、备份或 Git 内部目录。
需要持续协作和提交 PR 时请使用 Git clone / Fork；ZIP 是本次发布的交付快照。
