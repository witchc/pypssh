#!/bin/env python
import configparser, re, pprint
import paramiko
from pathlib import Path
import yaml
import logging
from pssh.clients import ParallelSSHClient
from typing import Union
# from scp import SCPClient
# from pssh.clients.native import ParallelSSHClient
from gevent import joinall
import click,gevent

logging.basicConfig(level = logging.ERROR,format = '%(asctime)s:%(name)s:%(levelname)s:%(message)s')
# logging.basicConfig(level = logging.DEBUG,format = '%(asctime)s:%(name)s:%(levelname)s:%(message)s')
logger = logging.getLogger(__name__)
config = configparser.ConfigParser(allow_no_value=True)
# 大小写不明感
IS_VARS=re.compile("(\w+):vars",re.I)
# 标准输入流
# stdin_text = click.get_text_stream('stdin')
# stdin_text.readable()

host_selected={}

# 返回为某个组的主机列表和主机配置
def conversion_config(config:dict, group:str = 'all') -> dict:
    host_groups = {key:dict(value) for key,value in config._sections.items() if key!='vars' and not re.match(IS_VARS,key) and key != 'DEFAULT'}
    vars_groups = {key:dict(value) for key,value in config._sections.items() if key=='vars' or re.match(IS_VARS,key) and key != 'DEFAULT'}
    host_groups.setdefault('all',[])

    # 处理其他组
    for item_group in host_groups:
        for host in host_groups[item_group]:
            # 将组变量合并到组主机
            host_groups[item_group][host] = {}
            host_groups[item_group][host].update(vars_groups.get('vars',{}))
            host_groups[item_group][host].update(vars_groups.get(item_group + ':vars',{}))
        host_groups['all'].update(host_groups[item_group])

    logger.debug('变量信息:\n' + pprint.pformat(vars_groups)) 
    logger.debug('最终主机组:\n' + pprint.pformat(host_groups))
    if group:
        return host_groups.get(group,{})
    else:
        return host_groups

def get_operate_target(config:dict, target:Union[list,str]) -> dict:
    group_target = conversion_config(config,None)
    host_target = { key:{key:dict(value)} for key,value in conversion_config(config, 'all').items() }
    return { **host_target, **group_target }.get(target,{})

@click.group()
@click.option('-i','--inventory',default='/etc/pypssh/inventory.conf',type=str,required=False)
@click.option('-d','--debug',flag_value=True,type=bool,required=False)
@click.argument('target', type = str, nargs = 1, required = True)
def cli(inventory, target, debug):
    if not Path(inventory).is_file():
        logger.error("%s 不是有效的配置文件" % inventory)
    config.read(inventory)
    global host_selected
    host_selected = get_operate_target(config,target)
    logger.debug("Host Selected is %s" % repr(host_selected))
    if debug:
        logger.setLevel(logging.DEBUG)


@cli.command()
def prints():
    print(host_selected)

@cli.command()
@click.option('-c','--command',default='echo hello world',type=str)
@click.option('--show/--no-show',default=True,type=bool)
@click.option('--pty',default=True,type=bool,required=False)
def execute(command, show, pty):
    client = ParallelSSHClient(list(host_selected.keys()),host_config=host_selected)
    output = client.run_command(command,use_pty=pty)
    client.join(output)
    logger.debug(output.items())
    for host, host_output in output.items():
        click.echo(click.style("- Host [%s]\n- Command [%s]"%(host,command),fg="green" if host_output.exit_code==0 else "red" ))
        if show:
            for line in host_output.stdout:
                click.echo("%s" % (line))
            for line in host_output.stderr:
                click.echo("%s" % (line))

@cli.command()
@click.argument('local_file', type=click.types.Path(exists=True))
@click.argument('remote_file', type=click.types.Path())
def put(local_file, remote_file):
    client = ParallelSSHClient(list(host_selected.keys()),host_config=host_selected)
    # greenlets = client.copy_file(local_file,remote_file,recurse=True)
    greenlets = client.scp_send(local_file,remote_file,recurse=True)
    joinall(greenlets, raise_error=False)

@cli.command()
@click.argument('remote_file', type=click.types.Path())
@click.argument('local_file', type=click.types.Path())
def pull(remote_file, local_file):
    client = ParallelSSHClient(list(host_selected.keys()),host_config=host_selected)
    # greenlets = client.copy_remote_file(remote_file,local_file,recurse=True)
    greenlets = client.scp_recv(remote_file,local_file,recurse=True)
    joinall(greenlets, raise_error=False)

@cli.command()
@click.option('-t','--timeout',default=1.0,type=float)
@click.option('-p','--port',default=22,type=int)
@click.option('--ssh-test/--no-ssh-test',default=True,type=bool)
def test(timeout, port, ssh_test):

    def _connect_test(host):
        s = gevent.socket.socket(gevent.socket.AF_INET, gevent.socket.SOCK_STREAM)
        s.timeout = timeout
        try:
            s.connect((host[0], port))
        except Exception as ex:
            logger.error(host[0] + ' 出现异常:' + repr(ex))
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
                timeout=timeout, 
                look_for_keys=False, 
                allow_agent=False,
                port=port)
        except Exception as ex:
            logger.error(host[0] + ' 出现异常:' + repr(ex))
            return None
        finally:
            client.close()
        return host

    all_hosts=[]
    all_hosts = list(host_selected.items())
    conns = [gevent.spawn(_ssh_test if ssh_test else _connect_test, host) for host in all_hosts]

    working_hosts = [s.get()[0] for s in gevent.joinall(conns) if s.get() is not None]
    all_hosts = [item[0] for item in all_hosts]

    print('Working_host：\n' + repr(set(working_hosts)))
    print('Non-Working_host：\n' + repr(set(all_hosts) - set(working_hosts)))


# 远程执行脚本文件，可以从配置文件加载变量，可以读参数变量
@cli.command()
@click.argument('script_file', type=click.types.Path())
@click.argument('script_arg', type = str, nargs = -1, required = False)
@click.option('-a','--arg',type=str,multiple=True,required=False)
@click.option('--show/--no-show',default=True,type=bool)
@click.option('--pty',default=True,type=bool,required=False)
@click.pass_context
def execfile(ctx, script_file, script_arg, arg, show, pty):
    ctx.invoke(put, local_file=script_file, remote_file="/tmp/.pypssh/tmp")
    script_env = ''.join(["export %s && "%item for item in arg])
    script_arg_str = ' '.join(script_arg)
    command = f"{script_env} chmod +x /tmp/.pypssh/tmp && /tmp/.pypssh/tmp {script_arg_str}"
    logger.debug(command)
    ctx.invoke(execute, command=command, show=show, pty=pty)


if __name__ == '__main__':
    cli()