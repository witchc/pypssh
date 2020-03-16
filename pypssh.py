#!/bin/env python
import configparser, re, pprint
from pathlib import Path
import yaml
import logging
from pssh.clients import ParallelSSHClient
# from scp import SCPClient
# from pssh.clients.native import ParallelSSHClient
from gevent import joinall
import click,gevent

logging.basicConfig(level = logging.ERROR,format = '%(asctime)s:%(name)s:%(levelname)s:%(message)s')
logger = logging.getLogger(__name__)
config = configparser.ConfigParser(allow_no_value=True)
# 大小写不明感
IS_VARS=re.compile("(\w+):vars",re.I)
# 标准输入流
# stdin_text = click.get_text_stream('stdin')
# stdin_text.readable()

# 返回为某个组的主机列表和主机配置
def conversion_config(config,group='all'):
    host_groups = {key:dict(value) for key,value in config._sections.items() if key!='vars' and not re.match(IS_VARS,key) and key != 'DEFAULT'}
    vars_groups = {key:dict(value) for key,value in config._sections.items() if key=='vars' or re.match(IS_VARS,key) and key != 'DEFAULT'}
    # 合并所有主机到 all group
    for i in host_groups.values():
        host_groups['all'].update(i)
    # 注入 all 变量
    for group in host_groups:
        for host in host_groups[group]:
            host_groups[group][host] = vars_groups.get('vars',{})
            host_groups[group][host].update(vars_groups.get(group + ':vars',{}))
    logger.debug('变量信息:\n' + pprint.pformat(vars_groups)) 
    logger.debug('最终主机组:\n' + pprint.pformat(host_groups))
    return host_groups

@click.group()
@click.option('-i','--inventory',default='/etc/pypssh/inventory.conf',type=str,required=False)
@click.option('-d','--debug',flag_value=True,type=bool,required=False)
def cli(inventory,debug):
    if not Path(inventory).is_file():
        logger.error("%s 不是有效的配置文件" % inventory)
    config.read(inventory)
    if debug:
        logger.setLevel(logging.DEBUG)

@cli.command()
@click.option('-g','--group',default='all',type=str)
@click.option('-h','--hosts',type=str,multiple=True,required=False)
@click.option('-c','--command',default='echo hello world',type=str)
@click.option('--show/--no-show',default=True,type=bool)
@click.option('--pty',default=True,type=bool,required=False)
def exec(group,hosts,command,show,pty):
    host_grouops = conversion_config(config)
    if hosts:
        hosts_config = {host:host_grouops.get('all',{}).get(host,{}) for host in hosts}
        client = ParallelSSHClient(list(hosts_config.keys()),host_config=hosts_config)
    else:
        client = ParallelSSHClient(list(host_grouops.get(group).keys()),host_config=host_grouops.get(group))
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
@click.option('-g','--group',default='all')
@click.option('-h','--hosts',type=str,multiple=True,required=False)
@click.argument('local_file', type=click.types.Path(exists=True))
@click.argument('remote_file', type=click.types.Path())
def put(group,hosts,local_file,remote_file):
    host_grouops = conversion_config(config)
    if hosts:
        hosts_config = {host:host_grouops.get('all',{}).get(host,{}) for host in hosts}
        client = ParallelSSHClient(list(hosts_config.keys()),host_config=hosts_config)
    else:
        client = ParallelSSHClient(list(host_grouops.get(group).keys()),host_config=host_grouops.get(group))
    # greenlets = client.copy_file(local_file,remote_file,recurse=True)
    greenlets = client.scp_send(local_file,remote_file,recurse=True)
    joinall(greenlets, raise_error=False)

@cli.command()
@click.option('-g','--group',default='all')
@click.option('-h','--hosts',type=str,multiple=True,required=False)
@click.argument('remote_file', type=click.types.Path())
@click.argument('local_file', type=click.types.Path())
def pull(group,hosts,remote_file,local_file):
    host_grouops = conversion_config(config)
    if hosts:
        hosts_config = {host:host_grouops.get('all',{}).get(host,{}) for host in hosts}
        client = ParallelSSHClient(list(hosts_config.keys()),host_config=hosts_config)
    else:
        client = ParallelSSHClient(list(host_grouops.get(group).keys()),host_config=host_grouops.get(group))
    # greenlets = client.copy_remote_file(remote_file,local_file,recurse=True)
    greenlets = client.scp_recv(remote_file,local_file,recurse=True)
    joinall(greenlets, raise_error=False)

@cli.command()
@click.option('-g','--group',default='all')
@click.option('-h','--hosts',type=str,multiple=True,required=False)
@click.option('-t','--timeout',default=1.0,type=float)
def test(group,hosts,timeout):
    host_grouops = conversion_config(config)
    def _connect(host):
        s = gevent.socket.socket(gevent.socket.AF_INET, gevent.socket.SOCK_STREAM)
        s.timeout = timeout
        try:
            s.connect((host, 22))
        except Exception as ex:
            logger.error(host + ' 出现异常:' + repr(ex))
            return None
        s.close()
        return host
    all_hosts=[]
    if hosts:
        all_hosts = list({host:host_grouops.get('all',{}).get(host,{}) for host in hosts})
        conns = [gevent.spawn(_connect, host) for host in all_hosts]
    else:
        all_hosts = list(host_grouops.get(group).keys())
        conns = [gevent.spawn(_connect, host) for host in all_hosts]
    working_hosts = [s.get() for s in gevent.joinall(conns) if s.get() is not None]
    print('Working_host：\n' + repr(set(working_hosts)))
    print('Non-Working_host：\n' + repr(set(all_hosts) - set(working_hosts)))

if __name__ == '__main__':
    cli()