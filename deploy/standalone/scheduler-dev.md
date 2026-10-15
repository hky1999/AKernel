# agent-dx 调度器的最小开发环境

节点物理操作日志固定在 `/home/akernel/adx/operations/sandboxd.sqlite`，与 Redis、
结果补交日志和检查点分开。节点启动必须配置 `sandboxd_operation_journal`；
重启保留日志及其锁文件，不能放进临时配置目录或通过删库处理未知创建结果。
这份日志记录物理操作事实，不替代统一宿主预算。

此配置在 BareMetal1 上建立单独的开发容器，不需要 Kubernetes、YuanRong、DataSystem、Kata、Firecracker 或 OpenTelemetry 收集器。它使用一个 Coordinator、一个节点服务、一个 Redis 和 gVisor。构建产物先冻结，镜像构建只安装依赖及校验输入 SHA256，不在部署时重新编译源码。

`builder/scheduler-dev.Dockerfile` 的构建上下文包含 `payload/`、根目录相对路径的 `SHA256SUMS` 和固定 AKernel 提交的 `source/`。`payload/opt/adx/current/` 是经过 `agent-dx/build/release/package.py verify` 验证的完整发布包，`payload/usr/local/bin/` 包含对应 sandboxd、sbox、完整 gVisor 发布包及静态 distill-fs。版本台账记录这些独立组件的完整提交、归档和二进制身份。

运行 `python3 deploy/standalone/scheduler-dev.py --image sha256:<完整镜像身份>` 查看计划；加 `--apply` 执行。启动器只创建 `akernel-agent-dx-dev` 网络与 `akernel-agent-dx-dev-node` 容器，状态位于 `/data/akernel-agent-dx-dev/state`。不自动删除已有容器、物理沙箱或预算数据。

容器限制为 4 核、16 GiB，CPU 绑定范围为 0–7，控制程序与运行时缓存预留 4 GiB。单个节点只通告固定的 3 核、12 GiB 和 16 GiB 磁盘容量。入口在受管理服务存活时每 10 秒核对原 cgroup 和状态目录、固定 CPU／内存上限、服务内存预留和最低磁盘剩余空间，再原子发布有效期为 60 秒的容量观测。来源目录被替换、上限变化或其他写入者修改容量文件时不续期，现有执行和对账继续运行，旧观测过期后节点关闭新任务准入；第二个续期者不能取得同一容量锁。展开部署后，入口将实际节点 ID 与固定容量一起写入 Coordinator 的 `host_budgets.BareMetal1`。这份容量与节点观测使用同一固定定义，但持久预算不随观测过期或节点离线消失。Coordinator 在开放服务前安装或核对配置，重启省略配置仍保留原账本，变更已安装的容量或归属会拒绝启动。本模板要求恰好一个 Coordinator 和一个节点；增加实际节点前须重新核对物理隔离、持久日志、端口和完整成员配置，不能通过复制节点通告来扩大宿主容量。匹配的 agent-dx 程序必须支持该配置与公开宿主预算视图；旧程序不能混用。此接线不等于多节点物理验收、独立动态额度或自动内存闭环，不支持动态超卖，不能作为高倍率吞吐证明。部署前运行 `python3 -m unittest discover -s deploy/standalone -p test_scheduler_capacity.py -v`，实际长期运行及失效关闭准入仍须在指定环境验收。

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
