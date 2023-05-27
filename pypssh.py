#!/bin/env python

import re
import os
import sys
import ast
import ssl
import time
import socket
import copy
import select
import logging
import platform
import itertools
import functools
import dataclasses
from click.core import V
import encodings.idna

from pathlib import Path
from dataclasses import asdict, dataclass, field
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures._base import as_completed
from typing import Optional, List, Callable, Dict

import yaml
import click
import paramiko
import marshmallow_dataclass

from yaml import Loader
from scp import SCPClient
from jinja2 import Template
from tenacity import retry, stop_after_attempt, after_log

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

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)
logger.removeHandler(logger.handlers)
logger.addHandler(logging.StreamHandler())

# 用于存储 pypssh 相关文件的主目录
MAIN_DIR = os.path.join(os.path.expanduser('~'), ".pypssh")
# 内建环境变量前缀
BUILDIN_ENV_PREFIX="PYPSSH"

# pattern
SLICE_PATTERN = "\[(\w*):(\w*)\]"
SLICE_NON_GROUP_PATTERN = "\[\w*:\w*\]"

SLICE_SPLIT_PATTERN = "\[((\w*):(\w*)|\w*)(,((\w*):(\w*)|\w*))*\]"
SLICE_NON_GROUP_PATTERN = "\[(?:(?:\w*):(?:\w*)|\w*)(?:,(?:(?:\w*):(?:\w*)|\w*))*\]"


BANNER_TIMEOUT = 300
RETRY_COUNT = 2
TARGET = []

class SSHException(Exception):
    def __init__(self, status):
        super().__init__(status)
        self.status = status

@dataclass
class Host:
    """
    host model
    """
    hostname: str = "localhost"
    username: str = "root"
    port: int = 22
    password: str = ""
    pkfile: str = ""
    pkpasswd: str = ""
    sudo: bool = False
    timeout: int = 5
    env: Dict[str,str] = field(default_factory=dict)
    tags: Dict[str, str] = field(default_factory=dict)

@dataclass
class SSHResult:
    hostname: str
    command: str
    stdout: Optional[str] = None
    stderr: Optional[str] = None
    returncode: int = 0
    exception: Optional[str] = None

@dataclass
class SCPResult:
    hostname: str
    src: str
    dst: str
    completed: bool = False
    exception: Optional[str] = None

class Evaluator(ast.NodeTransformer):

    def __init__(self, data) -> None:
        super().__init__()
        self.data = data

    def visit_UnaryOp(self, node):
        self.generic_visit(node)
        # print(ast.dump(node))
        unaryop = {
                ast.Not: lambda a: not a
            }.get(node.op.__class__)
        if isinstance(node.operand, ast.Name):
            return ast.Expr(unaryop(node.operand.id in self.data.keys()))
        else:
            return ast.Expr(unaryop(node.operand))


    def visit_Compare(self, node):
        self.generic_visit(node)
        # print(ast.dump(node))
        result = False
        lval = self.data.get(node.left.id) if hasattr(node.left,'id') else node.left.value
        for op, rnode in zip(node.ops, node.comparators):
            cmpop = {
                ast.Eq: lambda a, b: a == b,
                ast.In: lambda a, b: a in b,
                ast.Is: lambda a, b: a is b,
                ast.IsNot: lambda a, b: a is not b,
                ast.NotEq: lambda a, b: a != b,
                ast.NotIn: lambda a, b: a not in b,
            }.get(op.__class__)
            rval = rnode.id if hasattr(rnode,'id') else rnode.value
            if cmpop:
                result = cmpop(lval, rval)
                lval = rval
            else:
                raise NotImplementedError(
                    f"row {node.lineno} col {node.col_offset} error, {type(op).__name__} is an unsupported operation!")
        return ast.Expr(result)

    def visit_BoolOp(self, node):
        self.generic_visit(node)
        # print(f"boolop: {ast.dump(node)}")
        def bool_and_op(a, b):
            if not isinstance(a, bool):
                a =  self.visit_Expr(a).value
            if not isinstance(b, bool):
                b = self.visit_Expr(b).value
            return a and b
        def bool_or_op(a, b):
            if not isinstance(a, bool):
                a =  self.visit_Expr(a).value
            if not isinstance(b, bool):
                b = self.visit_Expr(b).value
            return a or b
        boolop = {
            ast.And: bool_and_op,
            ast.Or: bool_or_op,
        }.get(node.op.__class__)
        return ast.Expr(functools.reduce(boolop, [i for i in map(lambda v:v.value if hasattr(v,'value') else v , node.values)]))

    def visit_Expr(self, node):
        self.generic_visit(node)
        # print(f"expr: {ast.dump(node)}")
        if not hasattr(node, "value"):
            exist = node.id in self.data.keys()
            # print(exist, node.value.id, self.data.keys())
            return ast.Expr(exist)
        if isinstance(node.value, ast.Expr):
            return self.visit_Expr(node.value)
        elif isinstance(node.value, ast.Name):
            exist = node.value.id in self.data.keys()
            # print(exist, node.value.id, self.data.keys())
            return ast.Expr(exist)
        else:
            return ast.Expr(node.value)

    def eval(self, expr: str):
        tree = ast.parse(expr)
        tree = ast.fix_missing_locations(self.visit(tree))
        # print(f"result: {ast.dump(tree)}")
        if isinstance(tree.body[0].value, bool):
            return tree.body[0].value
        else:
            raise NotImplemented(tree)


def get_ssh_logger(host: Host):
    return logging.LoggerAdapter(logger=ssh_logger, extra=dataclasses.asdict(host))


def get_ssh_conn_client(host: Host):
    client = paramiko.SSHClient()
    # client.load_system_host_keys()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(
        hostname=host.hostname,
        username=host.username,
        port=host.port,
        password=host.password,
        passphrase=host.pkpasswd,
        timeout=host.timeout,
        banner_timeout=BANNER_TIMEOUT,
        # 需要能全局控制是否使用 agent
        # allow_agent=False,
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
        s_list = s[1:-1].split(",")
        for s_item in s_list:
            s_item_slice = f"[{s_item}]"
            if is_digest_slice(s_item_slice):
                s0, s1 = re.match(SLICE_PATTERN, s_item_slice).groups()
                slice_list.extend(list(range(int(s0), int(s1))))
            elif is_letter_slice(s_item_slice):
                s0, s1 = re.match(SLICE_PATTERN, s_item_slice).groups()
                slice_list.extend([chr(i) for i in range(ord(s0), ord(s1))])
            else:
                slice_list.append(s_item)
        slice_tuples.append((s, slice_list))
    return slice_tuples

def expand_hostname_slice(hostname:str) -> List[str]:
    find_slice = re.findall(SLICE_NON_GROUP_PATTERN, hostname)
    result = []
    if find_slice:
        slice_tuples = expand_slice(find_slice)
        combilist = [i for i in itertools.product(
                *[i[1] for i in slice_tuples])]
        for comitem in combilist:
            host = hostname
            for index, slice_key in enumerate([i[0] for i in slice_tuples]):
                host = host.replace(slice_key, str(comitem[index]), 1)
            result.append(host)
    return result

def render_hosts(rd:Dict) -> Dict[str, Host]:
    result = {}
    for key in rd.keys():
        hostnames = expand_hostname_slice(rd[key].get('hostname'))
        if hostnames:
            for hostname in hostnames:
                result[f"{key}-{hostname}"] = marshmallow_dataclass.class_schema(
                        Host)().load(rd[key])
                result[f"{key}-{hostname}"].hostname = hostname
        else:
            result[key] = marshmallow_dataclass.class_schema(
                Host)().load(rd[key])
    return result

# inventory plugins
def fillinghostname_fromkey(rd:Dict):
    result = copy.deepcopy(rd)
    for key in result.keys():
        if not result[key].get('hostname'):
            result[key]['hostname'] = key
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
    client = None
    try:
        lines = []
        returncode = 0
        client = get_ssh_conn_client(host)
        transport = client.get_transport()
        channel = transport.open_session()
        # set environment
        # envStr = f"export {BUILDIN_ENV_PREFIX}_HOSTNAME={host.hostname};"
        envStr=""
        for key, value in host.env.items():
            # 下面这行在某些环境不生效, 所以改成了命令的形式
            # channel.set_environment_variable(name=key, value=value)
            envStr+=f"export {key}={value};"
        # 必须获取 pty 才能有机会输入 sudo
        channel.get_pty()
        channel.update_environment(host.env)
        # 若是非阻塞则强制 recv 时会造成错误
        # channel.setblocking(0)
        channel.exec_command(envStr+command)
        if host.sudo:
            while channel.recv_ready() == False:
                stdout = channel.recv(4096)
                if re.search('\[sudo\] ', stdout.decode()):
                    channel.send(host.password+'\n')
                time.sleep(1)
        read_complate = False
        while not channel.exit_status_ready() or not read_complate:
            rl, _, _ = select.select([channel], [], [], 0.0)
            if len(rl) > 0:
                for line in linesplit(channel):
                    lines.append(line)
                    get_ssh_logger(host).info(line)
                read_complate = True
        returncode = channel.recv_exit_status()
    except Exception as ex:
        raise SSHException(SSHResult(hostname=host.hostname, command=command, stdout="\n".join(lines) if lines else "", returncode=returncode, exception=repr(ex)))
    finally:
        if client:
            client.close()
    return SSHResult(hostname=host.hostname, command=command, stdout="\n".join(lines) if lines else "", returncode=returncode)

def retry_logger(retry_state):
    rlog = logger
    exception = retry_state.outcome.exception()
    for arg in retry_state.args:
        if isinstance(arg, Host):
            rlog = get_ssh_logger(arg)
    rlog.warning(f"{exception} retry {retry_state.attempt_number}/{RETRY_COUNT}.")

def concurrent(func: Callable, tasks: list):
    tarfunc = retry(
            stop=stop_after_attempt(RETRY_COUNT), 
            after=retry_logger,
            retry_error_callback=lambda retry_state: retry_state.outcome.exception().status
        )(func)
    if not tasks and len(tasks) <= 0:
        return []
    with ThreadPoolExecutor(max_workers=len(tasks)) as executor:
        future_list = list()
        result_list = list()
        for task in tasks:
            future_list.append(executor.submit(tarfunc, *task))
        for future in as_completed(future_list):
            result_list.append(future.result())
        return result_list

def echo(output_mode:str, cls, datas, template):
    if output_mode == "none":
        return
    elif output_mode == "template":
        template = Template(template, lstrip_blocks=True, trim_blocks=True)
        datas.sort(key=lambda k: int(k.hostname.replace(".", "")))
        for r in datas:
            click.echo(template.render(dataclasses.asdict(r)))
    elif output_mode == "json":
        click.echo(marshmallow_dataclass.class_schema(
            cls)().dumps(datas, many=True))
    elif output_mode == "yaml":
        datas = [ dataclasses.asdict(item) for item in datas ]
        click.echo(yaml.dump(datas, allow_unicode=True))

def get_target(hosts: Dict[str, Host], name):
    result = []
    # session name
    if name in hosts.keys():
        result.append(hosts[name])
    # host name
    elif name in [ host.hostname for key, host in hosts.items() ]:
        result.extend(list(filter(lambda i: i.hostname==name, hosts.values())))
    # slice
    elif re.findall(SLICE_NON_GROUP_PATTERN, name):
        hostnames = expand_hostname_slice(name)
        result.extend(list(filter(lambda host:host.hostname in hostnames, hosts.values())))
    # tags
    else:
        for host in hosts.values():
            try:
                if Evaluator(host.tags).eval(name):
                    result.append(host)
            except Exception as ex:
                get_ssh_logger(host).debug(ex)
    return result

# version cmd
def get_resource_path(relative_path:str):
    if getattr(sys, 'frozen', False):
        base_path = sys._MEIPASS
    else:
        base_path = os.path.abspath(".")
    return os.path.join(base_path, relative_path)

def get_build_git_sha():
    filename = get_resource_path('BUILD_GITSHA')
    if not os.path.exists(filename):
        return 'unknown'
    with open(filename) as fh:
        return fh.read().strip()

def get_build_repo():
    filename = get_resource_path('BUILD_GITREPO')
    if not os.path.exists(filename):
        return 'unknown'
    with open(filename) as fh:
        repolist = [ tuple(i.split()) for i in fh.read().strip().split('\n') ]
        return repolist

def get_build_date():
    filename = get_resource_path('BUILD_DATE')
    if not os.path.exists(filename):
        return 'unknown'
    with open(filename) as fh:
        return fh.read().strip()

def get_last_commit_date():
    filename = get_resource_path('BUILD_LASTCOMMITDATE')
    if not os.path.exists(filename):
        return 'unknown'
    with open(filename) as fh:
        return fh.read().strip()

def print_version():
    """
    print version
    """
    addr = "https://github.com/witchc/pypssh"
    # version definition
    vno = "v0.2.4"
    interrupt_version = "Python " + ' '.join(sys.version.split('\n'))
    click.echo(
        "\n".join
        ([
            f"Github: {addr}",
            f"Version: {vno}",
            f"Running-Interrupt-Version: {interrupt_version}",
            f"Running-Platform: {platform.platform()}",
            f"OpenSSL version: {ssl.OPENSSL_VERSION}",
            f"BUILD_DATE: {get_build_date()}",
            f"BUILD_GIT_SHA: {get_build_git_sha()}",
            f"BUID_LAST_COMMIT_DATE: {get_last_commit_date()}",
            F"BUILD_GIT_REPO: {get_build_repo()}",
        ])
    )

def click_print_version(ctx, param, value):
    if value:
        print_version()
        ctx.exit()
    else:
        return
# root cmd

@click.group()
@click.option('-i', '--inventory', default=os.path.join(MAIN_DIR, "inventory", "inventory.yaml"), type=click.types.Path(), required=False, help="inventory.yaml path")
@click.option('-l', '--log-level', default='INFO', type=click.Choice(["NOTSET", "DEBUG", "INFO", "WARN", "ERROR", "FATAL"]), required=False)
@click.option('-t', '--target', type=str, required=False, help="Host IP address or label expression")
@click.option('-v', '--version', is_flag=True, callback=click_print_version, expose_value=False, is_eager=True, help="print program version")
def cli(inventory, log_level, target):
    hosts_dict = {}
    if Path(inventory).exists():
        with open(inventory) as file:
            temp = yaml.load(file, Loader=Loader)
            if temp and isinstance(temp, dict):
                hosts_dict = temp
    hosts_dict = fillinghostname_fromkey(hosts_dict)
    hosts = render_hosts(hosts_dict)
    ssh_logger.setLevel(log_level)
    global TARGET
    if target:
        TARGET = get_target(hosts, target)

# key management cmd

KEY_MANAGEMENT_PATH = os.path.join(MAIN_DIR, "keys")

@cli.group()
@click.option('-p', '--path', default=KEY_MANAGEMENT_PATH, type=click.types.Path(), required=False, help="key management path")
def key(path:str):
    """
    key management
    """
    KEY_MANAGEMENT_PATH = path
    os.makedirs(KEY_MANAGEMENT_PATH, exist_ok=True)

@key.command()
@click.argument('name', type=str, required=True)
@click.option('-p','--password', type=str)
def gen(name:str, password:Optional[str]=None):
    """
    key generate
    """
    if Path(KEY_MANAGEMENT_PATH).joinpath(name).exists():
        click.echo(f"key {name} already exist!")
        sys.exit(1)
    key = paramiko.RSAKey.generate(2048)
    try:
        with open(Path(KEY_MANAGEMENT_PATH).joinpath(name),"w+") as file:
            key.write_private_key(file, password)
        public_content = ["ssh-rsa", key.get_base64()]
        with open(Path(KEY_MANAGEMENT_PATH).joinpath(name).with_suffix(".pub"),"w+") as file:
            file.write(" ".join(public_content))
    except Exception as ex:
        if Path(KEY_MANAGEMENT_PATH).joinpath(name).exists():
            os.remove(Path(KEY_MANAGEMENT_PATH).joinpath(name))
        if Path(KEY_MANAGEMENT_PATH).joinpath(name).with_suffix(".pub").exists():
            os.remove(Path(KEY_MANAGEMENT_PATH).joinpath(name).with_suffix(".pub"))
        click.echo(f"key {name} generate faild! beacuase {ex}")
        sys.exit(1)
    click.echo(f"key {name} generate successful! public key is:\n{' '.join(public_content)}")

@key.command()
def ls():
    """
    key list
    """
    click.echo('date\t\t\tname')
    click.echo('----------------------------')
    for item in Path(KEY_MANAGEMENT_PATH).glob("*.pub"):
        time_sruct = time.localtime(item.stat().st_ctime)
        privekey = item.with_suffix("")
        if Path(privekey).exists():
            click.echo(f'{time.strftime("%Y--%m--%d %H:%M:%S", time_sruct)}\t{privekey.name}')

@key.command()
@click.argument('name', type=str, required=True)
def get(name:str):
    """
    Not implemented
    get key
    """
    click.echo("Not implemented")

@key.command()
@click.argument('name', type=str, required=True)
def trust(name:str):
    """
    Not implemented
    trust key
    """
    click.echo("Not implemented")

# config cmd
@cli.group()
def config():
    """
    configuration management
    """
    pass

default_config ="""
localhost:
  port: 22
  username: root
  password: youerpassword
  tags:
    yourtagkey: yourtagvalue
"""
@config.command()
def dump_default():
    """
    wirte default config
    """
    Path(MAIN_DIR).joinpath("inventory").mkdir(exist_ok=True, parents=True)
    with open(Path(MAIN_DIR).joinpath("inventory").joinpath("inventory.yaml"), "w") as f:
        f.write(default_config)
    click.echo(f'default config write {Path(MAIN_DIR).joinpath("inventory").joinpath("inventory.yaml")} successfully!')

def convert_config_for_010(config_file):

    import configparser
    IS_VARS = re.compile("((\w+):)?vars", re.I)
    configparserobj = configparser.ConfigParser(allow_no_value=True, delimiters=("="),strict=False)

    configparserobj.read(config_file)
    configparserobj.setdefault('vars',dict())
    configparserobj.setdefault('all',dict())

    # 获取所有主机组和值
    _host_groups = {key: dict(value) for key, value in configparserobj.items() if not re.match(IS_VARS, key)}
    # 获取所有主机组,设置值为空字典
    host_groups = {key: dict() for key in _host_groups.keys()}
    # 获取所有变量组
    vars_groups = {key: dict(value) for key, value in configparserobj.items() if re.match(IS_VARS, key)}

    # 处理主机组
    for item_group in _host_groups:
        for host_str in list(_host_groups[item_group]):
            _host = host_str.split(':')
            host = _host[0]

            # 将组变量合并到组主机
            host_groups[item_group][host] = dict()
            host_groups[item_group][host].update(vars_groups.get('vars', dict()))
            host_groups[item_group][host].update(vars_groups.get(item_group + ':vars', dict()))

            # 将内联变量合并到组主机
            if len(_host) > 1 and _host[1]:
                host_groups[item_group][host]['port'] = int(_host[1])
            if len(_host) > 2 and _host[2]:
                host_groups[item_group][host]['username'] = _host[2]
            if len(_host) > 3 and _host[3]:
                host_groups[item_group][host]['password'] = _host[3]

        # 将该组主机合并到 all 组中
        host_groups['all'].update(host_groups[item_group])
    
    # 转换为最新版本配置
    hosts = {}
    for tag, values in host_groups.items():
        for host, properties in values.items():
            hosts[f"{host}_{properties['username']}"] = dataclasses.asdict(Host(hostname=host, **properties, tags={tag:""}))
    print(yaml.dump(hosts, allow_unicode=True))


@config.command()
@click.option('-v', '--version', type=str, required=False, help="Config Parser Version.",default="0.1.0")
@click.argument('config_file', nargs=1, type=click.types.Path(), required=True)
def convert(version, config_file):
    """
    Load the old version of the config and convert to the current version of the config
    """
    if not Path(config_file).is_file():
        logger.warning("%s 不是有效的配置文件" % config_file)

    if version == "0.1.0":
        convert_config_for_010(config_file)
    else:
        logger.error("unsupported version!")


@config.command()
@click.argument('input', type=click.File('r'))
@click.option('-i', '--inventory', default=os.path.join(MAIN_DIR, "inventory", "inventory.yaml"), type=click.types.Path(), required=False, help="inventory.yaml path")
@click.option('-F', '--field-separator', type=str, required=False)
@click.option('-u', '--username', type=str, required=True)
@click.option('-p', '--passwd', type=str, required=True)
@click.option('-P', '--port', type=str, required=False, default=22)
@click.option('--pkfile', type=str, required=False, default="")
@click.option('--pkpasswd', type=str, required=False, default="")
@click.option('-s', '--sudo', type=str, required=False, default=False)
@click.option('-t', '--timeout', type=int, required=False, default=5)
@click.option('-e', '--env', type=str, required=False, multiple=True)
@click.option('-t', '--tag', type=str, required=False, multiple=True)
def add_host(input, inventory, field_separator, username, passwd, port, pkfile, pkpasswd, sudo, timeout, env, tag):
    """
    example:
        echo 172.18.40.40 | pypssh config add-host - -u root -p root
    """
    hostnames = input.read().split(sep=field_separator)
    _env = {}
    _tags = {}
    src_hosts = {}
    if Path(inventory).exists():
        with open(inventory) as file:
            temp = yaml.load(file, Loader=Loader)
            if temp and isinstance(temp, dict):
                src_hosts = temp

    for e in env:
        k, v = e.split("=")
        _env[k] = v
    for t in tag:
        if "=" in t:
            k, v = t.split("=")
            _tags[k] = v
        else:
            _tags[k] = ""

    for hostname in hostnames:
        host = Host(
            hostname=hostname,
            username=username,
            port=port,
            password=passwd,
            pkfile=pkfile,
            pkpasswd=pkpasswd,
            sudo=sudo,
            timeout=timeout,
            env=_env,
            tags=_tags
        )
        src_hosts[f"{hostname}_{username}"] = asdict(host)
    os.makedirs(Path(inventory).parent, exist_ok=True)
    with open(inventory, "w+") as file:
        yaml.dump(src_hosts, file)
    click.echo(f"hosts {hostnames} add successful!")

# 支持批量选择，条件选择多种选择方式对主机进行删除
# @config.command()
# @click.option('-i', '--inventory', default=os.path.join(MAIN_DIR, "inventory", "inventory.yaml"), type=click.types.Path(), required=False, help="inventory.yaml path")
# @click.option('-h', '--hostname', type=str, required=True)
# @click.option('-u', '--username', type=str, required=True)
# @click.option('-p', '--passwd', type=str, required=True)
# @click.option('-P', '--port', type=str, required=False, default=22)
# @click.option('--pkfile', type=str, required=False)
# @click.option('--pkpasswd', type=str, required=False)
# @click.option('-s', '--sudo', type=str, required=False, default=False)
# @click.option('-t', '--timeout', type=int, required=False, default=5)
# @click.option('-e', '--env', type=str, required=False, multiple=True)
# @click.option('-t', '--tag', type=str, required=False, multiple=True)
# def del_host(inventory, field_separator, username, passwd, port, pkfile, pkpasswd, sudo, timeout, env, tag):
#     """
#     example:
#         echo 172.18.40.40 | pypssh config add-host - -u root -p root
#     """
#     _env = {}
#     _tags = {}
#     hostnames = []
#     src_hosts = {}
#     if Path(inventory).exists():
#         with open(inventory) as file:
#             temp = yaml.load(file, Loader=Loader)
#             if temp and isinstance(temp, dict):
#                 src_hosts = temp

#     for e in env:
#         k, v = e.split("=")
#         _env[k] = v
#     for t in tag:
#         k, v = t.split("=")
#         _tags[k] = v
    
#     for session, host in src_hosts.items():
#         host = Host(**host)
#         if 

    
#     os.makedirs(Path(inventory).parent, exist_ok=True)
#     with open(inventory, "w+") as file:
#         yaml.dump(src_hosts, file)
#     click.echo(f"hosts {hostnames} del successful!")

# func cmd
"""
exec
"""

default_template = """
========== {{ hostname }} ==========
command: {{ command }}
{% if stdout %}
stdout:
{{stdout}}
{% endif %}
{% if stderr %}
stderr:
{{stderr}}
{% endif %}
returncode: {{ returncode }}
{% if exception %}
exception: {{ exception }}
{% endif %}
"""


@cli.command()
@click.option('-c', '--command', type=str, required=True)
@click.option('--needpty', default=True, type=bool, required=False)
@click.option('--sudo', default=False, type=bool, required=False)
@click.option('-o', '--outmode', default="none", type=click.Choice(["none", "template", "json", "yaml"]))
@click.option('-t', '--template', default=default_template, type=str, required=False)
def execute(command, needpty, sudo, outmode, template):
    """
    execute command
    """
    if sudo:
        for i in TARGET:
            i.sudo = sudo
    result = []
    if needpty:
        result = concurrent(
            realtime_output, [tuple([h, command]) for h in TARGET])
    else:
        raise NotImplementedError("not impl nonpty command!")
    echo(outmode, SSHResult, result, template)


@cli.command()
@click.argument('script_file', type=click.types.Path())
@click.argument('script_arg', type=str, nargs=-1, required=False)
@click.option('-e','--env', type=str, multiple=True, required=False)
@click.option('-a', '--attachment', type=str, multiple=True, required=False)
@click.option('-w', '--workdir', default='/root' ,type=str)
@click.option('--needpty', default=True, type=bool, required=False)
@click.option('--sudo', default=False, type=bool, required=False)
@click.option('-o', '--outmode', default="none", type=click.Choice(["none", "template", "json", "yaml"]))
@click.option('-t', '--template', default=default_template, type=str, required=False)
@click.pass_context
def execfile(ctx, script_file, script_arg, env, attachment, workdir, needpty, sudo, outmode, template):
    """
    execute script file
    """
    if not Path(script_file).is_file():
        raise AssertionError("script_file must is executable file!")
    remote_file = str(Path(workdir).joinpath(Path(script_file).name))
    put_files = []
    try:
        put_files.append(remote_file)
        ctx.invoke(put, local_file=script_file, remote_file=remote_file)
        for att_item in attachment:
            remote_att_file = str(Path(workdir).joinpath(Path(att_item).name))
            ctx.invoke(put, local_file=att_item, remote_file=remote_att_file)
            put_files.append(remote_att_file)
        script_env = ''.join(["export %s && " % item for item in env])
        script_arg_str = ' '.join(script_arg)
        command = f"{script_env} cd {workdir} && chmod +x {remote_file} && {remote_file} {script_arg_str}"
        ctx.invoke(execute, command=command, outmode=outmode, template=template, sudo=sudo)
    finally:
        command = f"rm -rf {' '.join(put_files)}"
        ctx.invoke(execute, command=command, outmode=outmode, template=template, sudo=sudo)

# put_default_template = "localhost:{{src}} =====> {{hostname}}:{{dst}} {% if completed %}successfully!{% else %} faild! because {{exception}} {% endif %}"
put_default_template = """
========== {{ hostname }} ==========
src: {{src}}
dst: {{dst}}
state: {% if completed %} successfully! {% else %} faild! {% endif %}  
{% if exception %}
exception: {{ exception }}
{% endif %}
"""

@cli.command()
@click.argument('local_file', type=click.types.Path(exists=True))
@click.argument('remote_file', type=click.types.Path())
@click.option('-o', '--outmode', default="none", type=click.Choice(["none", "template", "json", "yaml"]))
@click.option('-t', '--template', default=put_default_template, type=str, required=False)
@click.option('-r', '--recursive', default=True, type=bool)
@click.option('-p', '--process', default=False, type=bool)
def put(local_file, remote_file, outmode, template, recursive, process):
    """
    upload file
    example:
      pypssh -t 192.168.31.1 put /etc/yum.conf /etc/yum.conf
    """
    def _progress(filename, size, sent, peername):
        click.echo("(%s:%s) %s's progress: %.2f%%   \r" % (
            peername[0], peername[1], filename, float(sent)/float(size)*100))

    def _upload(host):
        try:
            ssh = get_ssh_conn_client(host)
            scp = SCPClient(ssh.get_transport(), progress4=_progress if process else None)
            scp.put(local_file, remote_file, recursive)
            get_ssh_logger(host).info(f"file localhost:{local_file} => {host.hostname}:{remote_file} successfully!")
            return SCPResult(host.hostname, src=local_file, dst=remote_file, completed=True)
        except Exception as ex:
            # get_ssh_logger(host).error(f"file localhost:{local_file} => {host.hostname}:{remote_file} faild! because {ex}")
            raise SSHException(SCPResult(host.hostname, src=local_file, dst=remote_file, completed=False, exception=repr(ex)))
    
    result = concurrent(_upload, [tuple([h]) for h in TARGET])
    echo(outmode, SSHResult, result, template)


# pull_default_template = "localhost:{{dst}} <===== {{hostname}}:{{src}} {% if completed %}successfully!{% else %} faild! because {{exception}} {% endif %}"
pull_default_template = """
========== {{ hostname }} ==========
src: {{src}}
dst: {{dst}}
state: {% if completed %} successfully! {% else %} faild! {% endif %}  
{% if exception %}
exception: {{ exception }}
{% endif %}
"""
@cli.command()
@click.argument('remote_file', type=click.types.Path())
@click.argument('local_file', type=click.types.Path())
@click.option('-r', '--recursive', default=True, type=bool)
@click.option('-o', '--outmode', default="none", type=click.Choice(["none", "template", "json", "yaml"]))
@click.option('-t', '--template', default=pull_default_template, type=str, required=False)
@click.option('-n', '--naming-template', default="{{ remote_file_name }}-{{ hostname }}", type=str)
@click.option('-p', '--process', default=False, type=bool)
def pull(remote_file, local_file, outmode, template, recursive, naming_template, process):
    """
    download file
    example:
      pypssh -t 192.168.31.1 put /etc/yum.conf /etc/yum.conf
    """
    def _progress(filename, size, sent, peername):
        click.echo("(%s:%s) %s's progress: %.2f%%   \r" % (
            peername[0], peername[1], filename, float(sent)/float(size)*100))

    def _download(host):
        try:
            ssh = get_ssh_conn_client(host)
            scp = SCPClient(ssh.get_transport(), progress4=_progress if process else None)
            if Path(local_file).is_dir():
                render_local_file = Path(local_file).joinpath(Template(naming_template).render(
                        {
                            **dataclasses.asdict(host), 
                            "local_file": local_file, 
                            "remote_file": remote_file, 
                            "remote_file_name": Path(remote_file).name, 
                            "local_file_name": Path(local_file).name
                        }))
                scp.get(
                    remote_file, 
                    render_local_file, 
                    recursive
                    )
            get_ssh_logger(host).info(f"file localhost:{render_local_file} <= {host.hostname}:{remote_file} successfully!")
            return SCPResult(host.hostname, src=remote_file, dst=render_local_file, completed=True)
        except Exception as ex:
            # get_ssh_logger(host).error(f"file localhost:{render_local_file} <= {host.hostname}:{remote_file} faild! because {ex}")
            raise SSHException(SCPResult(host.hostname, src=remote_file, dst=local_file, completed=False, exception=repr(ex)))

    result = concurrent(_download, [tuple([h]) for h in TARGET])
    
    echo(outmode, SSHResult, result, template)


@cli.command()
def ls():
    """
    list host
    """
    click.echo(yaml.dump([i.hostname for i in TARGET], allow_unicode=True))


@cli.command()
@click.option('--noicmp', type=bool, required=False, flag_value=False)
def ping(noicmp):
    """
    ping hosts
    """
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

    if not noicmp:
        c = [ i for i in concurrent(_connect_test, [tuple([i])
                               for i in TARGET]) if i]
    else:
        c = TARGET

    s = concurrent(_ssh_test, [tuple([i]) for i in c if i])

    working_host = [i.hostname for i in s if i]
    non_working_host = [i.hostname for i in TARGET if i not in s]
    working_host.sort(key=lambda key: int(key.replace(".", "")))
    non_working_host.sort(key=lambda key: int(key.replace(".", "")))
    result = {'working_host': working_host,
              'non_working_host': non_working_host}
    click.echo(yaml.dump(result, allow_unicode=True))

# version cmd
@cli.command()
def version():
    """
    print program version
    """
    print_version()



if __name__ == '__main__':
    cli()