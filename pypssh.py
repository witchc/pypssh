#!/bin/env python
import configparser
import re
import pprint
from string import Template
import paramiko
from pathlib import Path
import yaml
import logging
from pssh.clients import ParallelSSHClient
from typing import Union
# from scp import SCPClient
# from pssh.clients.native import ParallelSSHClient
from gevent import joinall
import click
import gevent
import json as jso

logging.basicConfig(level=logging.ERROR,format='%(asctime)s:%(name)s:%(levelname)s:%(message)s')
# logging.basicConfig(level = logging.DEBUG,format = '%(asctime)s:%(name)s:%(levelname)s:%(message)s')
logger = logging.getLogger(__name__)
config = configparser.ConfigParser(allow_no_value=True, delimiters=("="))
# 2020年6月17日 默认实现会将键转换为小写,这里改为不改变原来的键
config.optionxform = lambda option: option
# 大小写不明感
IS_VARS = re.compile("(\w+):vars", re.I)
# 标准输入流
# stdin_text = click.get_text_stream('stdin')
# stdin_text.readable()

host_selected = {}
num_retries=1
retry_delay=2

# 返回为某个组的主机列表和主机配置


def conversion_config(config:dict, group:str = 'all', username:str = None, password:str = None, port:int = None) -> dict:
    config.setdefault('vars',{})
    if username:
        config['vars']['username'] = username
    if password:
        config['vars']['password'] = password
    if port:
        config['vars']['port'] = port
    _host_groups = {key: dict(value) for key, value in config.items(
    ) if key != 'vars' and not re.match(IS_VARS, key) and key != 'DEFAULT'}
    host_groups = {key: {} for key, value in config.items(
    ) if key != 'vars' and not re.match(IS_VARS, key) and key != 'DEFAULT'}
    vars_groups = {key: dict(value) for key, value in config.items(
    ) if key == 'vars' or re.match(IS_VARS, key) and key != 'DEFAULT'}

    # 处理其他组
    for item_group in _host_groups:
        for host_str in list(_host_groups[item_group]):
            _host = host_str.split(':')
            host = _host[0]
            # 将组变量合并到组主机
            host_groups[item_group][host] = {}
            host_groups[item_group][host].update(vars_groups.get('vars', {}))
            host_groups[item_group][host].update(
                vars_groups.get(item_group + ':vars', {})
            )
            if len(_host) > 1 and _host[1]:
                host_groups[item_group][host]['port'] = int(_host[1])
            if len(_host) > 2 and _host[2]:
                host_groups[item_group][host]['username'] = _host[2]
            if len(_host) > 2 and _host[3]:
                host_groups[item_group][host]['password'] = _host[3]
        host_groups['all'].update(host_groups[item_group])

    logger.debug('变量信息:\n' + pprint.pformat(vars_groups))
    logger.debug('最终主机组:\n' + pprint.pformat(host_groups))
    if group:
        return host_groups.get(group, {})
    else:
        return host_groups

def get_operate_target(config:dict, target:str, username:str, password:str, port:int) -> dict:
    group_target = conversion_config(config, None, username, password, port)
    host_target = {key: {key: dict(value)} for key, value in conversion_config(
        config, 'all', username, password, port).items()}
    result = {**host_target, **group_target}.get(target, {})
    return result if result else {target:{'username':username,'password':password,'port':port}}

def get_client():
    return ParallelSSHClient(
                            list(host_selected.keys()), 
                            host_config=host_selected, 
                            num_retries=num_retries, 
                            retry_delay=retry_delay
                            )

@click.group()
@click.option('-i', '--inventory', default='/etc/pypssh/inventory.conf', type=str, required=False)
@click.option('-d', '--debug', flag_value=True, type=bool, required=False)
@click.option('-u', '--username', type=str, help="ssh帐号", required=False)
@click.option('-p', '--password', type=str, help="ssh密码", required=False)
@click.option('-P','--port',type=int,help='ssh端口',default=22,required=False)
@click.argument('target', type=str, nargs=1, required=True)
def cli(inventory, debug, username, password, port, target):
    """
    该脚本用于批量执行命令/脚本以及批量上传下载文件, 需要注意的是:
    \n
      - 上传下载文件不支持通配符，需要明确指定文件/目录
    \n
      - 使用 test / prints 可以测试端口/ssh连通性和目标选取到的数据
    """
    if not Path(inventory).is_file():
        logger.warning("%s 不是有效的配置文件" % inventory)
    try:
        config.read(inventory)
    except Exception as ex:
        logger.warning(ex)
    global host_selected
    host_selected = get_operate_target(config._sections, target, username, password, port)
    logger.debug("Host Selected is %s" % repr(host_selected))
    if debug:
        logger.setLevel(logging.DEBUG)


@cli.command()
def prints():
    """
    打印选择到的主机信息
    """
    print(host_selected)


@cli.command()
@click.option('-c', '--command', prompt='command', type=str, help="需要批量执行的命令")
@click.option('--json', flag_value=True, type=bool, required=False, help="将结果 json 化")
@click.option('--view', type=click.Choice(['fail', 'success'], case_sensitive=False), required=False, help="根据条件仅查看输出结果的一部分，目前的条件只支持命令是否执行成功")
@click.option('-t', '--template', 
              default="- Host: \n${host}\n- Command: \n${command}\n- Exception: \n${exstr}\n- STDOUT: \n${stdout}\n- STDERR: \n${stderr}\n- EXIT_CODE: \n${exit_code}\n", 
              type=str,help="python模版字符串,使用${var}能输出模板变量，目前支持的变量有host,command,exstr,stdout,stderr"
            )
def execute(command, json, view, template):
    """
    为目标批量执行命令
    """
    if json:
        logging.disable(50)
    client = get_client()
    output = client.run_command(command, stop_on_errors = False)
    client.join(output)
    logger.debug(output.items())
    results = []
    for host, host_output in output.items():
        exstr = repr(host_output.exception)
        stdout = '\n'.join([line for line in host_output.stdout]) if host_output.stdout else ''
        stderr = '\n'.join([line for line in host_output.stderr]) if host_output.stderr else ''
        exit_code = host_output.exit_code
        result_template = Template(template)
        result = {
            'host':host,
            'command':command,
            'exstr':exstr,
            'stdout':stdout,
            'stderr':stderr,
            'exit_code':exit_code
        }
        if view:
            if (view=="fail" and exit_code==0) or (view=="success" and exit_code!=0):
                continue

        if json:
            results.append(result)
        else:
            click.echo(result_template.substitute(result))
    if json:
        click.echo(jso.dumps(results,ensure_ascii=False))


@cli.command()
@click.argument('local_file', type=click.types.Path(exists=True))
@click.argument('remote_file', type=click.types.Path())
def put(local_file, remote_file):
    """
    为目标批量上传文件
    """
    client = get_client()
    # greenlets = client.copy_file(local_file,remote_file,recurse=True)
    greenlets = client.scp_send(local_file, remote_file, recurse=True)
    joinall(greenlets, raise_error=False)


@cli.command()
@click.argument('remote_file', type=click.types.Path())
@click.argument('local_file', type=click.types.Path())
def pull(remote_file, local_file):
    """
    为目标批量下载文件
    """
    client = get_client()
    # greenlets = client.copy_remote_file(remote_file,local_file,recurse=True)
    greenlets = client.scp_recv(remote_file, local_file, recurse=True)
    joinall(greenlets, raise_error=False)


@cli.command()
@click.option('-t', '--timeout', default=1.0, type=float)
@click.option('--json', flag_value=True, type=bool, required=False)
@click.option('--ssh-test/--no-ssh-test', default=True, type=bool)
def test(timeout, json, ssh_test):
    if json:
        logging.disable(50)
    """
    测试端口/ssh的连通性
    """
    def _connect_test(host):
        s = gevent.socket.socket(
            gevent.socket.AF_INET, gevent.socket.SOCK_STREAM)
        s.timeout = timeout
        try:
            s.connect((host[0], host[1]['port']))
        except Exception as ex:
            logger.error(f"{host[0]}:{host[1]['port']} 出现异常:{repr(ex)}")
            return None
        s.close()
        return host

    def _ssh_test(host):
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        try:
            client.connect(
                hostname=host[0],
                password=host[1]['password'],
                username=host[1]['username'],
                port= host[1]['port'],
                timeout=timeout,
                look_for_keys=False,
                allow_agent=False
                )
        except Exception as ex:
            logger.error(f"{host[0]}:{host[1]['port']} 出现异常: {repr(ex)}")
            return None
        finally:
            client.close()
        return host

    all_hosts = []
    all_hosts = list(host_selected.items())
    conns = [gevent.spawn(_ssh_test if ssh_test else _connect_test, host)
             for host in all_hosts]

    working_hosts = [s.get()[0]
                     for s in gevent.joinall(conns) if s.get() is not None]
    all_hosts = [item[0] for item in all_hosts]
    if json:
        click.echo(jso.dumps({'working_host':list(set(working_hosts)),'non_working_host':list(set(all_hosts) - set(working_hosts))}))
    else:
        click.echo('Working_host：\n' + repr(list(set(working_hosts))))
        click.echo('Non-Working_host：\n' + repr(list(set(all_hosts) - set(working_hosts))))


# 远程执行脚本文件，可以从配置文件加载变量，可以读参数变量
@cli.command()
@click.argument('script_file', type=click.types.Path())
@click.argument('script_arg', type=str, nargs=-1, required=False)
@click.option('--json', flag_value=True, type=bool, required=False)
@click.option('-t', '--template', 
              default="- Host: \n${host}\n- Exception: \n${exstr}\n- STDOUT: \n${stdout}\n- STDERR: \n${stderr}\n", 
              type=str,help="python模版字符串,使用${var}能输出模板变量，目前支持的变量有host,command,exstr,stdout,stderr"
            )
@click.option('-e','--env', type=str, multiple=True, required=False, help='脚本执行需要的环境变量')
@click.option('-a', '--attachment', type=str, multiple=True, required=False, help='执行脚本所需要的附属文件')
@click.option('-w','--workdir',default='/tmp/.pypssh/',type=str, help='工作区')
@click.pass_context
def execfile(ctx, script_file, json, template, script_arg, env, attachment, workdir):
    """
    使本地脚本文件批量下发到远程执行
    """
    if json:
        logging.disable(50)
    put_files = []
    if not Path(script_file).is_file():
        raise AssertionError("script_file must is file!")
    remote_file = str(Path(workdir).joinpath(Path(script_file).name))
    put_files.append(remote_file)
    ctx.invoke(put, local_file=script_file, remote_file=remote_file)
    for att_item in attachment:
        remote_att_file = str(Path(workdir).joinpath(Path(att_item).name))
        ctx.invoke(put, local_file=att_item, remote_file=remote_att_file)
        put_files.append(remote_att_file)
    logger.debug(f'put files is: {put_files}')
    script_env = ''.join(["export %s && " % item for item in env])
    script_arg_str = ' '.join(script_arg)
    command = f"{script_env} cd {workdir} && chmod +x {remote_file} && {remote_file} {script_arg_str} && rm -rf {' '.join(put_files)}"
    logger.debug(command)
    ctx.invoke(execute, command=command, json=json, template=template)


if __name__ == '__main__':
    cli()
