# agent-dx 调度器的最小开发环境

节点物理操作日志固定在 `/home/akernel/adx/operations/sandboxd.sqlite`，与 Redis、
结果补交日志和检查点分开。节点启动必须配置 `sandboxd_operation_journal`；
重启保留日志及其锁文件，不能放进临时配置目录或通过删库处理未知创建结果。
这份日志记录物理操作事实，不替代统一宿主预算。

此配置在 BareMetal1 上建立单独的开发容器，不需要 Kubernetes、YuanRong、DataSystem、Kata、Firecracker 或 OpenTelemetry 收集器。它使用一个 Coordinator、一个节点服务、一个 Redis 和 gVisor。构建产物先冻结，镜像构建只安装依赖及校验输入 SHA256，不在部署时重新编译源码。

`builder/scheduler-dev.Dockerfile` 的构建上下文包含 `payload/`、根目录相对路径的 `SHA256SUMS` 和固定 AKernel 提交的 `source/`。`payload/opt/adx/current/` 是经过 `agent-dx/build/release/package.py verify` 验证的完整发布包，`payload/usr/local/bin/` 包含对应 sandboxd、sbox、完整 gVisor 发布包及静态 distill-fs。版本台账记录这些独立组件的完整提交、归档和二进制身份。

运行 `python3 deploy/standalone/scheduler-dev.py --image sha256:<完整镜像身份>` 查看计划；加 `--apply` 执行。启动器只创建 `akernel-agent-dx-dev` 网络与 `akernel-agent-dx-dev-node` 容器，状态位于 `/data/akernel-agent-dx-dev/state`。不自动删除已有容器、物理沙箱或预算数据。

容器限制为 4 核、16 GiB，CPU 绑定范围为 0–7，控制服务的内存使用上限为4GiB，尚未证明独占预留。单个节点只通告固定的 3 核、12 GiB 和 16 GiB 磁盘容量。入口在受管理服务存活时每 10 秒核对原 cgroup 和状态目录、固定 CPU／内存上限、服务内存上限和最低磁盘剩余空间，再原子发布有效期为 60 秒的容量观测。来源目录被替换、上限变化或其他写入者修改容量文件时不续期，现有执行和对账继续运行，旧观测过期后节点关闭新任务准入；第二个续期者不能取得同一容量锁。展开部署后，入口将实际节点 ID 与固定容量一起写入 Coordinator 的 `host_budgets.BareMetal1`。这份容量与节点观测使用同一固定定义，但持久预算不随观测过期或节点离线消失。Coordinator 在开放服务前安装或核对配置，重启省略配置仍保留原账本，变更已安装的容量或归属会拒绝启动。本模板要求恰好一个 Coordinator 和一个节点；增加实际节点前须重新核对物理隔离、持久日志、端口和完整成员配置，不能通过复制节点通告来扩大宿主容量。匹配的 agent-dx 程序必须支持该配置与公开宿主预算视图；旧程序不能混用。此接线不等于多节点物理验收、独立动态额度或自动内存闭环，不支持动态超卖，不能作为高倍率吞吐证明。部署前运行 `python3 -m unittest discover -s deploy/standalone -p test_scheduler_capacity.py -v`，实际长期运行及失效关闭准入仍须在指定环境验收。

主机只在回环地址开放 19443 和 19080，分别映射 HTTPS 控制入口与 HTTP 数据入口。远程 SDK 使用独立 SSH 隧道访问，禁止复用其他实验入口。容器内部服务使用独立网络的网络隔离认证模式；公网入口仍验证管理员 API key。密钥只在首次启动时生成，写入权限为 0600 的持久文件，不进入日志和构建上下文。私有镜像仓库认证由操作员放入持久配置文件，不打进镜像。

首次运行必须验证实际父 cgroup 限制、每个进程的二进制 SHA256、创建与删除回读、命令和文件接口，以及手动暂停恢复的内存和文件状态。自动内存控制已接入本分支的显式配置；本配置的启动成功不代表资源调整或预算故障验收通过。

检查点使用 `/home/akernel/sandboxd/root/checkpoints/adx` 的真实持久目录，使节点存储位于 sandboxd 授权的检查点根目录之下。节点请求还必须携带检查点身份、保存可写文件系统的标志，并检查 sandboxd 明确返回完成成功；仅有公共 protobuf 字段兼容不足以证明暂停接口兼容。

启用自动内存开发配置时，在上述启动器命令中加 `--elastic-memory`。启动计划会明确加入
`AKERNEL_SCHEDULER_ELASTIC_MEMORY=true`；不传该参数时保持原有配置。入口只接受 `true`
或 `false`，拼写错误拒绝启动。开关同时安装 Coordinator 的 `memory_control` 和
sandboxd `[plugin.resource]` 的 `memory_high_ratio=0.75`，冲突配置拒绝启动。
该比例是 daemon 全局配置，固定额度沙箱也会获得相同比例的软节流；硬上限保持原语义。

中心增长阈值为当前上限的 65%，目标使用率为 50%；低于 30% 的观察持续 30 秒、
至少有 5 个有效采样才缩容。冷却期为 5 秒，采样最多保鲜 10 秒，操作期限为 10 秒。
只有显式使用 `ElasticMemoryPolicy(high_per_mille=750)` 的 runsc 沙箱参与自动调整；
内存实验固定每实例 CPU 为 500 millicores。出生 memory 是下界，mem_limit 是允许上界。
`memory.high` 只提供软节流，不保证避免 OOM。中心预算、节点额度、物理回读和清理仍须
使用匹配候选独立验收；结果未知继续保持占用，不能通过重启或换库消除预留。

该启动器仍只管理原有单节点开发环境，不能用开关修改已经存在的双节点测试容器。
部署前还应运行 `python3 -m unittest discover -s deploy/standalone -p test_scheduler_memory.py -v`。
生产控制程序、静态 Execd、SDK、平台入口与 sandboxd 字节必须在版本台账中匹配。

开发入口只在原有 sandboxd 子进程已经退出并被回收后，以相同命令和环境重新启动它。
重试间隔从一秒指数增加到最多三十秒，连续存活六十秒后重置。恢复期间保持原来的
`adx-service` 及容量来源检查，不删除物理状态、资源操作日志或持久预算；续期失败
仍等待原容量观测过期。首次启动只有 Unix socket 实际可连接后才启动调度服务，
残留 socket 文件不能作为就绪证据。传输可连接不等于资源对账或应用恢复已经通过。
关闭时不再开始下一轮重试，并按调度服务、当前 sandboxd 的顺序回收自己创建的子进程。
调度服务意外退出仍终止开发入口；此逻辑不检测仍存活但无响应的守护进程。

运行 `python3 -m unittest discover -s deploy/standalone -p test_scheduler_supervision.py -v`
验证真实自有子进程自行异常退出后的恢复、原控制进程与心跳连续性、容量续期失败、残留 socket 拒绝、
启动失败退避、调度服务失败和关闭边界。修改源码不会改变已经运行的旧容器或双节点
夹具。后者必须显式接入同一监督逻辑并重新冻结部署输入，随后独立验收原沙箱、资源
代际、操作及预算的恢复，才能声称实际 sandboxd 故障门通过。

## 恢复I/O统计的前置条件

`--elastic-memory`同时在sandboxd资源配置开启 `cgroup_io_accounting=true`，并要求开发入口以 `prepare_cgroup(io_accounting=True)` 在移动任何服务进程前验证io可用，再向下委派。sandboxd继续检查挂载根至沙箱父分组的每一层，保证沙箱叶可提供io.stat。默认关闭模式不要求io，不修改io.max；显式开启但缺少委派时必须拒绝，不能把缺失统计当作零。双节点实验夹具须显式传入同一布尔配置并匹配新入口SHA256，不能因默认参数兼容而绕过前置检查。

运行 `python3 -m unittest discover -s deploy/standalone -p test_scheduler_cgroup_io.py -v` 验证缺少io或namespace不符时尚未移动服务、正常委派和默认关闭行为。测试只写临时普通文件，不证明真实cgroup叶I/O计数、检查点／恢复峰值或原恢复卡点已经验收。


## 预算及执行历史的回收开关

`--history-reclamation` 显式加入 `AKERNEL_SCHEDULER_HISTORY_RECLAMATION=true`，入口把展开后的节点配置设为 `history_reclamation: true`。此开关默认关闭，与自动内存策略独立；环境值只接受 `true` 或 `false`。匹配的Coordinator、adxlet和sandboxd分别提供一致权威边界、持久确认意图和可续做的回收事务。新候选仍须在调度器版本台账登记源码、构建和物理验收，不能从配置测试推导已部署。

节点每30秒最多选择32个已经结算的终态。守护进程的清单容量不足时，节点把同一持久批次拆小重试；全部历史确认到达前保留原事实。当前执行、结果未知、当前确认内存与未决预留受到保护。中心完成操作保留64条并保存关闭下界和冷却时间信息，节点审计为每Runtime保留8条。退役回执和进度只有在原物理退役及中心预算释放分别成立后才回收，回收接口本身不释放预算。

启用后SQLite版本3、中心存储版本8和daemon创建准入索引版本2会拒绝旧二进制。停用周期回收不会擦除既有拒绝边界。重启必须保留原数据库、锁、权威边界和事务清单；不能删除这些文件来恢复准入。完整历史身份的恢复只使用同Runtime、相同创建与删除意图、相同退役generation的已完成原事实。缺失或损坏存储仍须保持拒绝，不把记录缺席当成物理退役。

配置检查使用 `python3 -m unittest discover -s deploy/standalone -p test_scheduler_history.py -v`。双节点验收夹具必须显式接入同一布尔开关及匹配入口摘要，不能把单节点启动器用作替换既有双节点环境的命令。

## 严格预算下的宿主压力准入

启动器增加可选 `--pressure-source /绝对目录/source.json`，默认不启用。它将来源文件所在目录只读挂载到 `/run/akernel-pressure`，传入 `AKERNEL_SCHEDULER_PRESSURE_SOURCE`，由入口给Coordinator配置 `host_pressure_admission.BareMetal1`、给节点配置同一 `host_pressure`。来源的观测路径必须位于该挂载目录，来源文件和观测文件不能同名。不能隐式停用已安装的压力契约；匹配中心安装后存储版本为9，重启必须提供相同来源。

宿主程序 `deploy/standalone/scheduler-pressure.py --config /持久目录/observer.json` 作为独立受监督进程运行；镜像也安装同一文件至 `/usr/local/bin/scheduler-pressure.py`。观察者必须位于本任务可证明归属的控制cgroup，计入实验父组，且位于任何故障目标之外。双节点使用一个共同父cgroup和一个观察者，不为节点复制物理容量。当前单节点启动器不自动创建宿主观察者或替换双节点部署。

观察者配置字段为 `identity`、`parent_path`、`parent_memory_max_bytes`、`parent_cpu_max`、`storage_pins`、`disk_path`、`output`、`valid_for_millis`、`sample_interval_millis` 和 `policy`。identity记录 `host_id`、`authority`、`boot_id`、`parent_device`、`parent_inode`、`storage_identity_sha256`；storage_pins逐项保存原目录或SQLite文件的path／device／inode，其排序保持的规范JSON摘要使用 `identity_digest()` 生成。来源文件使用相同identity，以及观测文件在容器中的path和max_age_millis。身份由部署者在隔离状态下生成，不能从不受信任的节点标签获取。

策略字段为 `memory_high_per_mille`、`memory_low_per_mille`、`psi_high_per_mille`、`psi_low_per_mille`、`disk_high_per_mille`、`disk_low_per_mille`、`recovery_millis` 和 `minimum_disk_available_bytes`。推荐严格开发配置为内存750／650、内存PSI20／5、磁盘900／800、连续低水位5000毫秒、最低磁盘余量16GiB，采样1000毫秒、有效期3000毫秒。PSI取some的avg10并换算为千分比。任一高水位或不足的磁盘余量立即关闸；全部回到低水位并持续满足恢复窗口后才开闸。首次启动、采样失败、超时和超过有效期的采样中断均重新关闭；一次采样必须在采样间隔以内完成，时间戳取采样开始时刻。

观察者使用排他文件锁及fsync后的原子替换，不覆盖软链接。来源目录、原父cgroup、CPU／内存上限、Linux启动身份或持久目录身份不符就发布关闭；进程被杀或发布失败后原文件自行过期。启动后修改观察者磁盘配置不会热更新已固定的身份和策略，须显式受控重启。掉电、丢盘或一致旧备份恢复必须先隔离和人工核对权威；没有备份外不可回退水位，不能宣称自动检测原地回退。

资源账目将出生额度、已确认额外额度、在途额外预留分别登记；未知结果及恢复占用作为交叉视图。未授权的允许上限不计作已经取得的容量。各节点服务内存上限不是独占预留，父组与12GiB预算的差额也不是独占控制或恢复保证。当前独占控制、检查点和恢复应急预留均为零；观察者和控制服务的实际使用计入父组，物理峰值独立记录。压力关闭不改变原预算，缩容和精确结算继续执行，扩容授权仍有原deadline和每步至多两倍的策略增长约束。

运行 `python3 -I -B -m unittest discover -s deploy/standalone -p 'test_scheduler*.py' -v` 验证配置、压力与既有容量、内存、I/O、监督边界。真实阈值、关闸时延、正值预算保持、恢复拒绝、服务重启及最终库存仍须对匹配候选独立验收。

节点完整可用性仍限制新增自动策略。压力关闭允许既有授权缩容及精确结算，不使缺少容量或生命周期就绪证明的节点获得新自动授权。

消费端同样直接检查所在Linux宿主的boot ID，拒绝旧启动期间仍未到期的观测。本配置只覆盖同宿主中心和节点，跨宿主观测传输与验证另行开发。

## 独立整链准入观察

scheduler-pressure.py的私有配置可增加runtime字段，其中pool固定path、device、inode、memory_max_bytes、high_bytes、low_bytes、psi_high_per_mille及psi_low_per_mille，guard固定path、identity和max_age_millis。此字段由操作者显式加入独立宿主观察配置，默认启动配置不会自动启用。

pool读取真实memory.max、memory.current及memory.pressure，不替换原宿主、父分组或磁盘指标。guard.identity包含host_id、boot_id、parent_device、parent_inode、profile_sha256、pool_device和pool_inode；观察文件包含schema=1、identity、正sequence、32位十六进制control_session、observed_unix_millis、valid_until_unix_millis、admission_open和reason。文件须为有界普通文件且不能组写或全员写。开放时reason为ready。缺失、过期、身份或子池上限变化时关闭准入；显式关闭、子池高水位及PSI只会收紧原宿主门，恢复仍须连续低水位窗口。

现有对外schema1及八项真实metrics保持，额外整链状态留在独立文件。该观察不授权资源操作，也不证明已经拥有应急驻留容量。启用前必须在同一持久预算中预留服务与峰值、验证独立应急页及增长和回收边界；物理结论在调度器VERSION.md单独登记。
