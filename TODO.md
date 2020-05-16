# Pypssh
这里使用 [TODO.md Kanban Board](https://marketplace.visualstudio.com/items?itemName=coddx.coddx-alpha) 插件提供的任务面板能力进行简单的任务跟踪。

### Todo
- [易用性需求] 目前用 root 用户不能 pypssh 到 app 用户，这需要测试和改进认证。
- [易用性需求] 使文件传输支持 glob 表达式。
- [代码优化] 新增关键DEBUG输出，为调试提供更好的支持。
- [安全性需求] 为 inventory.conf 新增加密支持，并且列出或者使用时需要输入认证参数才能使用。

### In Progress


### Done
- [易用性需求:2020年5月16日] 支持无需配置文件的使用，在命令行中使用 -u -p -P 选项传入用户名，密码和端口
- [易用性需求:2020年5月16日] execute, execfile, test 子命令可输出 json
- [易用性需求:2020年4月26日] execfile 指令支持携带脚本参数。
- [易用性需求] 将输出以及输入信息对象化，能使用用户提供的模版语法进行输出。fix:直接捕获局部变量渲染提供的模版字符串(;简单粗暴)


