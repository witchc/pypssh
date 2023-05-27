#!/usr/bin/env sh
# 修改密码
echo root:root | chpasswd
# 创建 ssh 密钥对
ssh-keygen -t rsa -f ~/.ssh/id_rsa -N ''
# 信任公钥
cat ~/.ssh/id_rsa.pub >> ~/.ssh/authorized_keys