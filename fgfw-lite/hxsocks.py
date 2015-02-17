#!/usr/bin/env python
# coding:utf-8
import struct
import encrypt
import io
import time
import hashlib
import logging
logger = logging.getLogger('FW_Lite')
from collections import defaultdict
from threading import RLock
try:
    import urllib.parse as urlparse
except ImportError:
    import urlparse
from basesocket import basesocket
from parent_proxy import ParentProxy
from dh import DH

default_method = 'rc4-md5'
keys = {}
newkey_lock = defaultdict(RLock)


class hxssocket(basesocket):
    bufsize = 8192

    def __init__(self, hxsServer=None, ctimeout=1, parentproxy=None, iplist=None):
        basesocket.__init__(self)
        if hxsServer and not isinstance(hxsServer, ParentProxy):
            hxsServer = ParentProxy(hxsServer, hxsServer)
        self.hxsServer = hxsServer
        self.timeout = ctimeout
        if parentproxy and not isinstance(parentproxy, ParentProxy):
            parentproxy = ParentProxy(parentproxy, parentproxy)
        self.parentproxy = parentproxy
        if self.hxsServer:
            self.PSK = urlparse.parse_qs(self.hxsServer.parse.query).get('PSK', [''])[0]
            self.method = urlparse.parse_qs(self.hxsServer.parse.query).get('method', [''])[0] or default_method
            self.serverid = (self.hxsServer.parse.username, self.hxsServer.parse.hostname)
        self.cipher = None
        self.connected = 0
        # value: 0: request not sent
        #        1: request sent, no server response received
        #        2: server response received

    def connect(self, address):
        self.getKey()
        if self._sock is None:
            from connection import create_connection
            p = self.hxsServer.parse
            host, port = p.hostname, p.port
            self._sock = create_connection((host, port), self.timeout, self.timeout + 2, parentproxy=self.parentproxy, tunnel=True)
            self.pskcipher = encrypt.Encryptor(self.PSK, self.method)
        self._address = ('%s:%s' % address).encode()
        self.setsockopt = self._sock.setsockopt
        self.fileno = self._sock.fileno

    def getKey(self):
        with newkey_lock[self.serverid]:
            if self.serverid not in keys:
                for _ in range(2):
                    p = self.hxsServer.parse
                    host, port, usn, psw = (p.hostname, p.port, p.username, p.password)
                    if self._sock is None:
                        from connection import create_connection
                        self._sock = create_connection((host, port), self.timeout, self.timeout + 2, parentproxy=self.parentproxy, tunnel=True)
                        self.pskcipher = encrypt.Encryptor(self.PSK, self.method)
                    dh = DH()
                    pubk = dh.getPubKey()
                    data = chr(0) + struct.pack('>I', int(time.time())) + struct.pack('>H', len(pubk)) + pubk + hashlib.sha256(pubk + usn.encode() + psw.encode()).digest()
                    self._sock.sendall(self.pskcipher.encrypt(data))
                    fp = self._sock.makefile('rb')
                    resp = ord(self.pskcipher.decrypt(fp.read(self.pskcipher.iv_len + 1)))
                    if resp == 0:
                        pklen = struct.unpack('>H', self.pskcipher.decrypt(fp.read(2)))[0]
                        server_key = self.pskcipher.decrypt(fp.read(pklen))
                        auth = self.pskcipher.decrypt(fp.read(32))
                        if auth == hashlib.sha256(pubk + server_key + usn + psw).digest():
                            shared_secret = dh.genKey(server_key)
                            keys[self.serverid] = (hashlib.md5(pubk).digest(), shared_secret)
                            return
                        logger.error('hxsocket getKey Error: server auth failed')
                    else:
                        fp.read(ord(self.pskcipher.decrypt(fp.read(1))))
                        logger.error('hxsocket getKey Error. bad password or timestamp.')

    def recv(self, size):
        if self.connected == 0:
            self.sendall(b'')
        if self.connected == 1:
            fp = self._sock.makefile('rb')
            resp_len = 1 if self.pskcipher.decipher else self.pskcipher.iv_len + 1
            # now don't need to worry pskcipher iv anymore.
            if ord(self.pskcipher.decrypt(fp.read(resp_len))) != 0:
                fp.read(ord(self.pskcipher.decrypt(fp.read(1))))
                if self.serverid in keys:
                    del keys[self.serverid]
                logger.error('hxsocket Error: invalid shared key.')
                # TODO: it is possible to reconnect here.
                return b''
            fp.read(ord(self.pskcipher.decrypt(fp.read(1))))
            self.connected = 2
        buf = self._rbuffer
        buf.seek(0, 2)  # seek end
        buf_len = buf.tell()
        self._rbuffer = io.BytesIO()  # reset _rbuf.  we consume it via buf.
        if buf_len == 0:
            # Nothing in buffer? Try to read.
            data = self._sock.recv(self.bufsize)
            if not data:
                return b''
            data = self.cipher.decrypt(data)
            if len(data) <= size:
                return data
            buf_len = len(data)
            buf.write(data)
            del data  # explicit free
        buf.seek(0)
        rv = buf.read(size)
        if buf_len > size:
            self._rbuffer.write(buf.read())
        return rv

    def sendall(self, data):
        if self.connected == 0:
            self.cipher = encrypt.Encryptor(keys[self.serverid][1], self.method)
            self._sock.sendall(self.pskcipher.encrypt(chr(1) + keys[self.serverid][0]) + self.cipher.encrypt(struct.pack('>I', int(time.time())) + chr(len(self._address)) + self._address + data))
            self.connected = 1
        else:
            self._sock.sendall(self.cipher.encrypt(data))

    def dup(self):
        new = hxssocket()
        new.hxsServer = self.hxsServer
        new.timeout = self.timeout
        new.parentproxy = self.parentproxy
        new._sock = self._sock.dup()
        new.cipher = self.cipher
        new.pskcipher = self.pskcipher
        new.PSK = self.PSK
        new.connected = self.connected
        new._rbuffer = self._rbuffer
        new.method = self.method
        new.serverid = self.serverid
        return new

if __name__ == '__main__':
    hxs = hxssocket('hxs://user:pass@127.0.0.1:80')
    hxs.connect(('www.baidu.com', 80))
    hxs.sendall(b'GET / HTTP/1.0\r\n\r\n')
    data = hxs.recv(1024)
    while data:
        print(repr(data))
        data = hxs.recv(1024)
