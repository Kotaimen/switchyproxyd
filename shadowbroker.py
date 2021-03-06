#!/usr/bin/env python

import gevent
import gevent.monkey

gevent.monkey.patch_all()

import json
import socket
import select
import SocketServer
import struct
import logging

import pprint

# ===============================================================================
# Config
# ===============================================================================

import selectproxy

CONFIG = None


def lookup_upstream(proxy):
    return (
    CONFIG['upstreams'][proxy]['addr'], CONFIG['upstreams'][proxy]['port'])


# ===============================================================================
# Socks5
#===============================================================================


def send_all(sock, data):
    bytes_sent = 0
    while True:
        r = sock.send(data[bytes_sent:])
        if r < 0:
            return r
        bytes_sent += r
        if bytes_sent == len(data):
            return bytes_sent


class Socks5Handler(SocketServer.StreamRequestHandler):
    def handle(self):
        try:
            self.do_handle()
        except Exception:
            logging.exception('Unexpected error')
        finally:
            self.connection.close()

    def do_handle(self):
        sock = self.connection
        client_address = self.client_address
        logging.info('Connected from %s:%d', *client_address)
        # 1. Version
        sock.recv(262)
        sock.sendall(b"\x05\x00");
        # 2. Request
        data = self.rfile.read(4)
        if not data:
            logging.error("Aborted: %s:%d didn't say anyting", *client_address)
            return
        mode = ord(data[1])
        addrtype = ord(data[3])
        if addrtype == 1:  # IPv4
            addr = socket.inet_ntoa(self.rfile.read(4))
        elif addrtype == 3:  # Domain name
            addr = self.rfile.read(ord(sock.recv(1)[0]))
        port = struct.unpack('>H', self.rfile.read(2))

        if mode != 1:
            reply = b"\x05\x07\x00\x01"  # Command not supported
            sock.sendall(reply)
            logging.error('Only supports SOCKS no auth')
            return

        reply = b"\x05\x00\x00\x01"
        logging.info('Accepted  %s:%d => %s:%d', client_address[0],
                     client_address[1], addr, port[0], )

        # Open upstream
        remote = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        remote.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            proxy = selectproxy.select_proxy(addr)
            logging.debug('Host "%s" is %s', addr, proxy)
            if proxy in ('LOCAL', 'DOMESTIC'):
                remote.connect((addr, port[0]))
                local = remote.getsockname()
                logging.info('Direct %s:%d => %s %s:%d => %s:%d',
                             client_address[0], client_address[1],
                             proxy, local[0], local[1],
                             addr, port[0])
                reply += socket.inet_aton(local[0]) + struct.pack(">H",
                                                                  local[1])
                sock.sendall(reply)
            else:
                remote.connect(lookup_upstream(proxy))
                local = remote.getsockname()
                logging.info('Proxy %s:%d => %s %s:%d => %s:%d',
                             client_address[0], client_address[1],
                             proxy, local[0], local[1],
                             addr, port[0])
                reply += socket.inet_aton(local[0]) + struct.pack(">H",
                                                                  local[1])
                remote.sendall(b"\x05\x01\x00")
                data = remote.recv(262)
                if addrtype == 1:
                    tosend = b"\x05\x01\x00\x01" + socket.inet_aton(addr)
                elif addrtype == 3:
                    tosend = b"\x05\x01\x00\x03" + struct.pack('B', len(
                        addr)) + bytes(addr)
                    tosend += struct.pack('>H', port[0])
                remote.sendall(tosend)
                data = remote.recv(262)
                sock.sendall(reply)
        except socket.error:
            logging.exception('Socket error while connecting')
            sock.sendall('\x05\x05\x00\x01\x00\x00\x00\x00\x00\x00')
            remote.close()
            return

        # 3. Transfering
        try:
            total_sent, total_read = self.do_handle_tcp(sock, remote)
        except socket.error:
            logging.exception('Socket error while tranfering')
        else:
            logging.info(
                'Connection %s:%r closed, %d bytes read, %d bytes sent',
                client_address[0], client_address[1],
                total_read, total_sent)
        finally:
            remote.close()

    def do_handle_tcp(self, sock, remote):
        total_sent, total_read = 0, 0
        while True:
            rlist, w, e = select.select([sock, remote], [], [])
            if sock in rlist:
                data = sock.recv(4096)
                if len(data) <= 0:
                    break
                sent = send_all(remote, data)
                total_sent += sent
                if sent < len(data):
                    break
            if remote in rlist:
                data = remote.recv(4096)
                if len(data) <= 0:
                    break
                read = send_all(sock, data)
                total_read += read
                if read < len(data):
                    break
        return total_sent, total_read


#===============================================================================
# Entry
#===============================================================================


class TCPServer(SocketServer.ThreadingMixIn, SocketServer.TCPServer):
    allow_reuse_address = True


def main():
    global CONFIG
    CONFIG = json.load(open('config/shadowbroker.json'))

    format = '%(asctime)-15s %(name)s %(levelname)s %(message)s'
    logging.basicConfig(level=logging.INFO,
                        format=format)

    print 'Listening on %s:%d' % (CONFIG['addr'], CONFIG['port'])
    print 'Config:'
    pprint.pprint(CONFIG)

    server = TCPServer((CONFIG['addr'], CONFIG['port']), Socks5Handler)
    server.serve_forever()


if __name__ == '__main__':
    main()

