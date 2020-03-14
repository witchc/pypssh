
## parallel-ssh简介
该工具包有如下特性：
- 只传入主机列表的情况下，默认使用系统自带的 ssh 代理/私钥去获取SSH连接。
- 传入主机列表和用户名以及密码获取SSH连接。
- 传入主机列表和主机配置(为每台主机提供不同的配置比如用户名和密码)也可以获取SSH连接。
- 不使用系统自带的默认私钥，而是使用程序指定 ssh 私钥。
- 编程 ssh 代理，自己指定多个私钥。
- 命令执行结果字典格式为:
    ```python
    {
        ip:{
            host,
            exit_code,
            cmd,
            channel,
            stdout,
            stderr,
            stdin,
            exception
        }
    }
    ```
- 有多个客户端可以相互替代使用，这些客户端一般在兼容性，性能上有一定区别。
    - 默认客户端：from pssh.clients import ParallelSSHClient
    - 本地客户端：from pssh.clients.native import ParallelSSHClient
- 支持基于隧道的连接，也就是支持 ParallelSSHClient->代理主机——–>目标主机 这种情况的连接。

## click 简介
该工具包支持 Options, Arguments 两种类型的Parameters，其中选项是指可有可无的，带短横线的参数，参数是指有固定位置的参数。该包还提供了很多使用的命令行工具包(比如终端打印，流获取，按键，编辑器，文件等)，它以函数为中心，使用装饰器定义函数所需要的参数和选项等。

Parameters支持的类型在定义时可以使用`type={type} | type=({type1}...{typen})`进行指定，它支持以下预定义类型(也可以自定义类型)：
- str / click.STRING:
- int / click.INT:
- float / click.FLOAT:
- bool / click.BOOL:
- click.UUID:
- class click.File(mode='r', encoding=None, errors='strict', lazy=None, atomic=False)
- class click.Path(exists=False, file_okay=True, dir_okay=True, writable=False, readable=True, resolve_path=False, allow_dash=False, path_type=None)
- class click.Choice(choices, case_sensitive=True)
- class click.IntRange(min=None, max=None, clamp=False)
- class click.FloatRange(min=None, max=None, clamp=False)
- class click.DateTime(formats=None)

Options支持：
命名：没有名称的参数当做 Options 的名字，比如 `@click.option('-s','--string')` 该选项就有两个名字。
必须：`required=True`
默认值：`default=1`或者`default=callback()`设置静态的或者动态的默认值，动态的默认值通常使用`lambda表达式进行计算`，该选项还可以加上`show_default=True`选项使帮助显示默认值。
多值：使用 `nargs={int}` 可以指定选项的次数，使用 `multiple=True` 可以使接受的参数变成列表或元祖。
计数：使用`count=True`可以记录该参数的次数。
布尔标志：使用斜线间隔两个参数可以使这对参数变成布尔标志，此时选项会隐式传递`is_flag=True`。比如`--shout/--no-shout`。
提示：使用`prompt="提示字符串"`定义提示字符串，当用户没有输入时会出现自动要求用户输入。这种交互的提示性输入还能附带`hide_input=True`隐藏输入，以及附带`confirmation_prompt=True`让用户确认输入(两次输入)。这在输入密码时很有效。
确认：
环境变量取值：可以通过`envvar='USERNAME'`取得`USERNAME`环境变量赋值给参数。同时环境变量取值可以配合`multiple=True`使参数自动封装成列表/元祖的形式。

Arguments支持：
多值：使用 `nargs={int}` 可以指定选项的次数，当该值为`-1`则接受无限量的参数，当该值为`+`时接受至少一个参数。这可以使接受的参数变成列表或元祖。

### 命令和组
- 使用`@click.pass_context`装饰器能使处理函数的第一个参数变成 `click.context` ,该参数记录了命令执行的状态和参数等信息。


pyinstaller打包命令:
```bash
pyinstaller pypssh.py -F --hidden-import=ssh2.agent --hidden-import=ssh2.pkey --hidden-import=ssh2.utils --hidden-import=ssh2.channel --hidden-import=ssh2.sftp_handle --hidden-import=ssh2.listener --hidden-import=ssh2.statinfo --hidden-import=ssh2.knownhost --hidden-import=ssh2.sftp --hidden-import=ssh2.sftp_handle --hidden-import=ssh2.session --hidden-import=ssh2.publickey --hidden-import=ssh2.fileinfo --hidden-import=ssh2.exceptions --hidden-import=ssh2.error_codes --hidden-import=ssh2.c_stat --hidden-import=ssh2.ssh2 --hidden-import=ssh2.c_sftp --hidden-import=ssh2.c_pkey --hidden-import=ssh2.agent
```


## 参考
[click文档](https://click.palletsprojects.com/en/7.x/)