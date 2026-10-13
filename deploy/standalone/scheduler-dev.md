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

首次运行必须验证实际父 cgroup 限制、每个进程的二进制 SHA256、创建与删除回读、命令和文件接口，以及手动暂停恢复的内存和文件状态。已有内存扩展尚未接入 agent-dx 控制面；本配置的启动成功不代表资源调整或预算故障验收通过。

检查点使用 `/home/akernel/sandboxd/root/checkpoints/adx` 的真实持久目录，使节点存储位于 sandboxd 授权的检查点根目录之下。节点请求还必须携带检查点身份、保存可写文件系统的标志，并检查 sandboxd 明确返回完成成功；仅有公共 protobuf 字段兼容不足以证明暂停接口兼容。
