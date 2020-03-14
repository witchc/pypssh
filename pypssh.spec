# -*- mode: python ; coding: utf-8 -*-

block_cipher = None


a = Analysis(['pypssh.py'],
             pathex=['/root/pypssh'],
             binaries=[],
             datas=[],
             hiddenimports=['ssh2.agent', 'ssh2.pkey', 'ssh2.utils', 'ssh2.channel', 'ssh2.sftp_handle', 'ssh2.listener', 'ssh2.statinfo', 'ssh2.knownhost', 'ssh2.sftp', 'ssh2.sftp_handle', 'ssh2.session', 'ssh2.publickey', 'ssh2.fileinfo', 'ssh2.exceptions', 'ssh2.error_codes', 'ssh2.c_stat', 'ssh2.ssh2', 'ssh2.c_sftp', 'ssh2.c_pkey', 'ssh2.agent'],
             hookspath=[],
             runtime_hooks=[],
             excludes=[],
             win_no_prefer_redirects=False,
             win_private_assemblies=False,
             cipher=block_cipher,
             noarchive=False)
pyz = PYZ(a.pure, a.zipped_data,
             cipher=block_cipher)
exe = EXE(pyz,
          a.scripts,
          a.binaries,
          a.zipfiles,
          a.datas,
          [],
          name='pypssh',
          debug=False,
          bootloader_ignore_signals=False,
          strip=False,
          upx=True,
          upx_exclude=[],
          runtime_tmpdir=None,
          console=True )
