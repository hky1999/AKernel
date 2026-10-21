# 在 ADX 中使用受控共享卷

先安装固定源码版本的 `adx-environment` SDK，再安装本源码版本的 `akernel-sdk`。即使 PyPI 包的版本字符串相同，也不保证包含这些实验扩展。每次实验都应保留源码提交清单。

`SharedVolumes` 用于创建持久化的逻辑卷，并管理所有者与被授权者的权限；`SharedVolumeMount` 用于将经过准入检查的目录挂入沙箱。它们既不是公开 checkpoint 目录服务，也不是允许任意宿主路径挂载的 API。

```python
import uuid
from akernel_sdk import SharedVolumes

with SharedVolumes() as volumes:
    volume = volumes.create(request_id=str(uuid.uuid4()))
    volume = volumes.grant(
        volume.id,
        "YOUR-READER-TENANT",
        expected_permission_revision=volume.permission_revision,
        access="read_only",
    )
```

身份认证使用的 API key 决定卷所有者，请求不能另选所有者。若创建结果不确定，重试时应沿用同一个请求 ID，不能直接换新 ID 盲目重试。权限版本按整数处理；过期版本的更新会被拒绝，不会放宽权限检查。

在卷所有者的 SDK 进程中：

```python
from akernel_sdk import Sandbox, SharedVolumeMount

writer = Sandbox(
    runtime="runsc",
    failover=True,
    shared_volumes=[SharedVolumeMount(
        volume_id=volume.id, target="/mnt/shared", access="read_write"
    )],
)
writer.files.write("/mnt/shared/message.txt", "message from the writer")
writer.commands.run("sync")
```

读者应在另一个进程中运行，使用自己的 `AKERNEL_TOKEN` 和同一个经过 TLS 校验的服务端点，以 `access="read_only"` 挂载。跨节点验收还应明确选择不同的源/目标节点 ID，并确认实际落点；文件读取成功本身不能证明发生了跨节点访问。

当前已验收的策略是一个读写者配合多个只读者，不代表通用的多写者并发更新协议已经通过验收。已测试基本的关闭写入后由新读者读取，以及显式拒绝向只读 EROFS 写入；不能据此推断已经缓存文件的读者能够立即看到所有变更。

共享卷保存实时应用数据，与沙箱私有可写层不同，checkpoint 不会将其回滚。恢复准入会重新检查存储身份和权限；检查失败时不能悄悄替换为本地目录。

撤销权限会阻止新的挂载准入，但不代表已有运行中挂载已经停止。SDK 的正常删除返回，也不足以单独证明可以回收底层文件，必须先完成与具体运行时执行实例绑定的退出确认。关闭 `SharedVolumes` 仅释放客户端，不删除持久化卷。

gVisor 和 Firecracker 使用相同的公共共享卷挂载契约；各自的私有根文件系统和内存恢复机制仍保持运行时差异。实际验收过的版本组合，以 CrossNode 交付清单和验收范围说明为准。
