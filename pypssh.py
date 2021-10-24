#!/bin/env python

import re
import os
import sys
import glob
import time
import socket
import pprint
import select
import logging
import platform
import itertools
import dataclasses
from enum import Enum
from pathlib import Path
from dataclasses import dataclass, field

from typing import Union, Optional, List, Callable, Dict
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures._base import as_completed

import yaml
import click
import paramiko
import marshmallow_dataclass
from yaml import Loader

ssh_formatter = logging.Formatter(
    fmt=f'%(asctime)s [%(hostname)s][%(levelname)s] %(message)s')
ssh_streamHandler = logging.StreamHandler()
ssh_streamHandler.setFormatter(ssh_formatter)
# logging.basicConfig(level=logging.ERROR,format='%(asctime)s:%(name)s:%(levelname)s:%(message)s')
# logging.basicConfig(level = logging.DEBUG,format = '%(asctime)s:%(name)s:%(levelname)s:%(message)s')
ssh_logger = logging.getLogger("ssh")
ssh_logger.setLevel(logging.INFO)
ssh_logger.removeHandler(ssh_logger.handlers)
ssh_logger.addHandler(ssh_streamHandler)

MAIN_DIR = os.path.join(os.path.expanduser('~'), ".pypssh")
SLICE_PATTERN = "\[(\w*):(\w*)\]"
SLICE_NON_GROUP_PATTERN = "\[\w*:\w*\]"
BANNER_TIMEOUT = 300
TARGET = []


@dataclass
class Host:
    """
    主机类
    """
    hostname: str = "localhost"
    username: str = "root"
    port: int = 22
    password: str = ""
    pkfile: str = ""
    pkpasswd: str = ""
    sudo: bool = False
    timeout: int = 1
    tags: Dict[str, str] = field(default_factory=dict)


@dataclass
class SSHResult:
    stdout: Optional[str] = None
    stderr: Optional[str] = None
    returncode: int = 0
    exception: Optional[Exception] = None
    content: Union[bytes, str, None] = None


def get_ssh_logger(host: Host):
    return logging.LoggerAdapter(logger=ssh_logger, extra=dataclasses.asdict(host))


def get_ssh_conn_client(host: Host):
    client = paramiko.SSHClient()
    client.load_system_host_keys()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(
        hostname=host.hostname,
        username=host.username,
        password=host.password,
        passphrase=host.pkpasswd,
        timeout=host.timeout,
        banner_timeout=BANNER_TIMEOUT,
        key_filename=host.pkfile if host.pkfile else None
    )
    return client


def is_digest_slice(s: str):
    if not re.match(SLICE_PATTERN, s):
        return False
    a, b = s[1:-1].split(":")
    try:
        return re.match("\d+", a).group() == a and re.match("\d+", b).group() == b
    except Exception:
        return False


def is_letter_slice(s: str):
    if not re.match(SLICE_PATTERN, s):
        return False
    a, b = s[1:-1].split(":")
    try:
        return re.match("\w+", a).group() == a and re.match("\w+", b).group() == b
    except Exception:
        return False


def expand_slice(slices: list) -> list:
    slice_tuples = []
    for s in slices:
        slice_list = []
        if is_digest_slice(s):
            s0, s1 = re.match(SLICE_PATTERN, s).groups()
            slice_list = list(range(int(s0), int(s1)))
        elif is_letter_slice(s):
            s0, s1 = re.match(SLICE_PATTERN, s).groups()
            slice_list = [chr(i) for i in range(ord(s0), ord(s1))]
        else:
            raise AssertionError("slice error " + s)
        slice_tuples.append((s, slice_list))
    return slice_tuples


def load_hosts(config_path: str) -> Dict[str, Host]:
    result = {}
    with open(config_path) as file:
        hosts = yaml.load(file, Loader=Loader)
        for key in hosts.keys():
            find_slice = re.findall(SLICE_NON_GROUP_PATTERN, key)
            if find_slice:
                slice_tuples = expand_slice(find_slice)
                combilist = [i for i in itertools.product(
                    *[i[1] for i in slice_tuples])]
                for comitem in combilist:
                    host = key
                    for index, slice_key in enumerate([i[0] for i in slice_tuples]):
                        host = host.replace(slice_key, str(comitem[index]), 1)
                    result[host] = marshmallow_dataclass.class_schema(
                        Host)().load(hosts[key])
                    result[host].hostname = host
            else:
                result[key] = marshmallow_dataclass.class_schema(
                    Host)().load(hosts[key])
    return result

# execute utils


def linesplit(socket):
    buffer_bytes = socket.recv(4048)
    buffer_string = buffer_bytes.decode()
    done = False
    while not done:
        if "\n" in buffer_string:
            (line, buffer_string) = buffer_string.split("\n", 1)
            yield line
        else:
            more = socket.recv(4048)
            if not more:
                done = True
            else:
                buffer_string = buffer_string + more.decode()
    if buffer_string:
        yield buffer_string


def realtime_output(host: Host, command: str):
    client = get_ssh_conn_client(host)
    try:
        transport = client.get_transport()
        channel = transport.open_session()
        # 必须获取 pty 才能有机会输入 sudo
        channel.get_pty()
        # 若是非阻塞则强制 recv 时会造成错误
        # channel.setblocking(0)
        channel.exec_command(command)
        lines = []
        if host.sudo:
            while channel.recv_ready() == False:
                stdout = channel.recv(4096)
                if re.search('\[sudo\] ', stdout.decode()):
                    channel.send(host.password+'\n')
                time.sleep(1)
        while True:
            rl, _, _ = select.select([channel], [], [], 0.0)
            if len(rl) > 0:
                for line in linesplit(channel):
                    lines.append(line)
                    get_ssh_logger(host).info(line)
    except Exception as ex:
        pass
    finally:
        client.close()
    return "\n".join(lines)


def concurrent(func: Callable, tasks: list):
    if not tasks and len(tasks) <= 0:
        return []
    with ThreadPoolExecutor(max_workers=len(tasks)) as executor:
        future_list = list()
        result_list = list()
        for task in tasks:
            future_list.append(executor.submit(func, *task))
        for future in as_completed(future_list):
            result_list.append(future.result())
        return result_list


def get_target(hosts, name):
    result = []
    result.append(hosts[name])
    return result

# root cmd


@click.group()
@click.option('-i', '--inventory', default=os.path.join(MAIN_DIR, "inventory", "inventory.yaml"), type=str, required=False)
@click.option('-l', '--log-level', default='INFO', type=click.Choice(["NOTSET", "DEBUG", "INFO", "WARN", "ERROR", "FATAL"]), required=False)
@click.option('-t', 'target', type=str, required=False)
@click.pass_context
def cli(ctx, inventory, log_level, target):
    hosts = load_hosts(inventory)
    ssh_logger.setLevel(log_level)
    global TARGET
    TARGET = get_target(hosts, target)


# config cmd
"""
TODO 通过命令行编辑主机配置文件
@cli.group()
def config():
    pass

@config.command()
def add_host():
    pass

@config.command()
def del_host():
    pass
"""

# func cmd
"""
exec
"""


@cli.command()
def execute():
    pass


@cli.command()
def execfile(ctx, script_file, json, template, script_arg, env, attachment, workdir):
    pass


@cli.command()
@click.argument('local_file', type=click.types.Path(exists=True))
@click.argument('remote_file', type=click.types.Path())
def put(local_file, remote_file):
    """
    example:\n
      pypssh -t 192.168.31.1 put /etc/yum.conf /etc/yum.conf
    """
    pass


@cli.command()
@click.argument('remote_file', type=click.types.Path())
@click.argument('local_file', type=click.types.Path())
def pull(remote_file, local_file):
    """
    example:\n
      pypssh -t 192.168.31.1 pull /etc/yum.conf /etc/yum.conf
    """
    pass


@cli.command()
def ls():
    click.echo(yaml.dump([i.hostname for i in TARGET]))


@cli.command()
def ping():
    def _connect_test(host: Host):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(host.timeout)
        try:
            s.connect((host.hostname, host.port))
        except Exception as ex:
            get_ssh_logger(host).error(
                f"connection {host.port} test exception: {ex}")
            return None
        finally:
            s.close()
        return host

    def _ssh_test(host):
        try:
            client = get_ssh_conn_client(host)
        except Exception as ex:
            get_ssh_logger(host).error(f"ssh test exception: {ex}")
            return None
        return host

    c = [i for i in concurrent(_connect_test, [tuple([i])
                               for i in TARGET]) if i]
    s = concurrent(_ssh_test, [tuple([i]) for i in c])
    result = {'working_host': [i.hostname for i in s], 'non_working_host': [
        i.hostname for i in TARGET if i not in s]}
    click.echo(yaml.dump(result))


@cli.command()
def version():
    addr = "https://github.com/witchc/pypssh"
    vno = "v0.2.0"
    interrupt_version = "Python " + ' '.join(sys.version.split('\n'))
    print(
        "\n".join
        ([
            f"地址: {addr}",
            f"版本号: {vno}",
            f"解释器版本: {interrupt_version}",
            f"发行版: {platform.platform()}"
        ])
    )


if __name__ == '__main__':
    cli()
    # print(concurrent(realtime_output, [(Host(hostname="",username="",password="",sudo=True),"sudo tail -f /all.log"),(Host(hostname="",username=",password=",sudo=True),"sudo tail -f all.log")]))
    # pprint.pprint(load_hosts("config/inventory.yaml"))
