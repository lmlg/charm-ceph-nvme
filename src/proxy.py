#! /usr/bin/env python3
#
# Copyright 2024 Canonical Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#  http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import argparse
from collections import namedtuple
import json
import logging
import os
import pickle
import socket
import sys
import tempfile
import uuid

sys.path.append(os.path.dirname(os.path.abspath(__name__)))
import utils


NQN_BASE = 'nqn.2014-08.org.nvmexpress:uuid:'
RPCHandler = namedtuple('RPCHandler', ['expand', 'post'])

logger = logging.getLogger(__name__)


def _json_dumps(x):
    return json.dumps(x, separators=(',', ':'))


class ProxyError(Exception):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)


class ProxyCommand:
    def __init__(self, msg):
        self.msg = msg

    def __call__(self, proxy):
        return proxy.msgloop(self.msg)


class ProxyAddListener:
    def __init__(self, msg):
        self.msg = msg

    def __call__(self, proxy):
        msg = self.msg.copy()
        params = msg['params']['listen_address']
        port, adrfam = utils.get_free_port(params['traddr'])
        params['adrfam'] = str(adrfam)
        params['trsvcid'] = str(port)
        return proxy.msgloop(msg)


class ProxyAddHost:
    def __init__(self, msg, key):
        self.msg = msg
        self.key = key

    def __call__(self, proxy):
        if not self.key:
            return proxy.msgloop(self.msg)

        with tempfile.NamedTemporaryFile(mode='w+') as file:
            file.write(self.key)
            file.flush()
            msg = self.msg.copy()
            msg['params']['psk'] = file.name
            return proxy.msgloop(msg)


class Proxy:
    def __init__(self, port, cmd_file, xaddr, rpc_path):
        self.rpc_sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.rpc_sock.connect(rpc_path)
        self.receiver = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.receiver.bind(('0.0.0.0', port))
        self.cmd_file = open(cmd_file, 'a+b')
        self.buffer = bytearray(4096 * 10)
        self.rpc = utils.RPC()
        self.xaddr = xaddr
        self.handlers = {
            'create': RPCHandler(self._expand_create, self._post_create),
            'remove': RPCHandler(self._expand_remove, None),
            'cluster_add': RPCHandler(self._expand_cluster_add, None),
            'join': RPCHandler(self._expand_join, None),
            'find': RPCHandler(self._expand_default, self._post_find),
            'leave': RPCHandler(self._expand_leave, None),
            'list': RPCHandler(self._expand_default, self._post_list),
            'host_add': RPCHandler(self._expand_host_add, None),
            'host_del': RPCHandler(self._expand_host_del, None),
            'host_list': RPCHandler(self._expand_default, self._post_host_list)
        }

        for cmd in self._prepare_file():
            self._process_cmd(cmd)

    def get_spdk_subsystems(self):
        """Return a dictionary describing the subsystems for the gateway."""
        obj = self.msgloop(self.rpc.nvmf_get_subsystems())
        if not isinstance(obj, dict):
            logger.warning('did not receive a dict from SPDK: %s', obj)
            return

        obj = obj.get('result', ())
        ret = {}
        for elem in obj:
            nqn = elem.pop('nqn')
            if nqn is None or 'discovery' in nqn:
                continue

            ret[nqn] = elem

        return ret

    def _write_cmd(self, cmd):
        pickle.dump(cmd, self.cmd_file)
        self.cmd_file.flush()

    def _process_cmd(self, cmd):
        obj = cmd(self)
        if not isinstance(obj, dict):
            logger.error('invalid response received (%s - %s)',
                         type(obj), obj)
            raise TypeError()
        elif 'error' in obj:
            logger.error('error running command: %s', obj)
            raise ProxyError(obj['error'])

        return obj

    def _prepare_file(self):
        """Read the contents of the bootstrap file or set it up it if empty."""
        size = self.cmd_file.tell()
        if not size:
            # File is empty.
            logger.info('SPDK file is empty; starting bootstrap process')
            payload = self.rpc.nvmf_create_transport(trtype='tcp')
            cmd = ProxyCommand(payload)
            self._write_cmd(cmd)
            yield cmd
        else:
            self.cmd_file.seek()
            while True:
                try:
                    yield pickle.load(self.cmd_file)
                except EOFError:
                    break

    def msgloop(self, msg):
        """Send an RPC to SPDK and receive the response."""
        self.rpc_sock.sendall(json.dumps(msg).encode('utf8'))
        nbytes = self.rpc_sock.recv_into(self.buffer)
        try:
            return json.loads(self.buffer[:nbytes])
        except Exception:
            return None

    def handle_request(self, msg, addr):
        """Handle a request from a particular client."""
        obj = json.loads(msg)
        method = obj['method'].strip()
        if method == 'stop':
            logger.debug('stopping proxy as requested')
            return True

        handler = self.handlers.get(method)
        if handler is None:
            logger.error('invalid method: %s', method)
            self.receiver.sendto(('{"error": "invalid method: %s"}' %
                                 method).encode('utf8'), addr)
            return

        logger.info('processing request: %s', obj)
        obj = obj.get('params')
        cmds = list(handler.expand(obj))
        for cmd in cmds:
            self._process_cmd(cmd)

        # Only write the commands after they've succeeded.
        for cmd in cmds:
            self._write_cmd(cmd)

        resp = {}
        if handler.post is not None:
            resp = handler.post(obj)
        self.receiver.sendto(_json_dumps(resp).encode('utf8'), addr)

    @staticmethod
    def _make_exc_msg(exc):
        if isinstance(exc, ProxyError):
            return exc.args[0]

        return {"code": -2, "type": str(type(exc)), "message": str(exc)}

    def serve(self):
        """Main server loop."""
        while True:
            inaddr = None
            try:
                nbytes, inaddr = self.receiver.recvfrom_into(self.buffer)
                logger.info('got a request from address ', inaddr)
                rv = self.handle_request(self.buffer[:nbytes], inaddr)
                if rv:
                    logger.warning('got a request to stop proxy')
                    return
            except Exception as exc:
                logger.exception('caught exception: ')
                if inaddr is not None:
                    err = {"error": self._make_exc_msg(exc)}
                    self.receiver.sendto(json.dumps(err).encode('utf8'),
                                         inaddr)

    # RPC handlers.

    @staticmethod
    def _parse_bdev_name(name):
        ix = name.find('://')
        ret = json.loads(name[ix + 3:])
        ret['type'] = name[:ix]
        return ret

    @staticmethod
    def _ns_dict(bdev_name, nqn):
        # In order for namespaces to be equal, the following must match:
        # namespace ID (always set to 1)
        # NGUID (32 bytes)
        # EUI64 (16 bytes)
        # UUID
        # The latter 3 are derived from the NQN, which is either allocated
        # on the fly, or passed in as a parameter.
        uuid = nqn[len(NQN_BASE):]
        base = uuid.replace('-', '')
        return dict(bdev_name=bdev_name, nsid=1, nguid=base,
                    eui64=base[:16], uuid=uuid)

    @staticmethod
    def _subsystem_to_dict(subsys):
        elem = subsys['listen_addresses'][0]
        return {'addr': elem['traddr'], 'port': elem['trsvcid'],
                **Proxy._parse_bdev_name(subsys['namespaces'][0]['name'])}

    def _expand_default(self, _):
        return []

    def _expand_create(self, msg):
        cluster = msg['cluster']
        bdev = {'pool': msg['pool_name'], 'image': msg['rbd_name'],
                'cluster': cluster}
        bdev_name = 'rbd://' + _json_dumps(bdev)

        nqn = msg.get('nqn')
        if nqn is None:
            nqn = NQN_BASE + str(uuid.uuid4())
            msg['nqn'] = nqn   # Inject it to use it in the post handler.

        payload = self.rpc.bdev_rbd_create(
            name=bdev_name, pool_name=msg['pool_name'],
            rbd_name=msg['rbd_name'],
            cluster_name=cluster, block_size=4096)
        yield ProxyCommand(payload)

        payload = self.rpc.nvmf_create_subsystem(
            nqn=nqn, ana_reporting=True, max_namespaces=2)
        yield ProxyCommand(payload)

        payload = self.rpc.nvmf_subsystem_add_listener(
            nqn=nqn,
            listen_address=dict(trtype='tcp', traddr=self.xaddr))
        yield ProxyAddListener(payload)

        payload = self.rpc.nvmf_subsystem_add_ns(
            nqn=nqn,
            namespace=self._ns_dict(bdev_name, nqn))
        yield ProxyCommand(payload)

    def _post_create(self, msg):
        subsystems = self.get_spdk_subsystems()
        nqn = msg['nqn']
        sub = subsystems[nqn]
        lst = sub['listen_addresses'][0]
        return {'addr': lst['traddr'], 'nqn': nqn, 'port': lst['trsvcid']}

    def _expand_remove(self, msg):
        nqn = msg['nqn']
        name = self.get_spdk_subsystems()[nqn]['namespaces'][0]['name']
        payload = self.rpc.nvmf_subsystem_remove_ns(
            nqn=msg['nqn'], nsid=1)
        yield ProxyCommand(payload)

        payload = self.rpc.nvmf_delete_subsystem(nqn=msg['nqn'])
        yield ProxyCommand(payload)

        payload = self.rpc.bdev_rbd_delete(name=name)
        yield ProxyCommand(payload)

    def _expand_cluster_add(self, msg):
        payload = self.rpc.bdev_rbd_register_cluster(
            name=msg['name'], user_id=msg['user'],
            config_param={'key': msg['key'], 'mon_host': msg['mon_host']})
        yield ProxyCommand(payload)

    def _expand_join(self, msg):
        nqn = msg['nqn']
        subsystems = self.get_spdk_subsystems()
        if nqn not in subsystems:
            return

        for elem in msg.get('addresses', ()):
            payload = self.rpc.nvmf_discovery_add_referral(
                subnqn=nqn, address=dict(
                    trtype='tcp', traddr=elem['addr'],
                    trsvcid=str(elem['port'])))
            yield ProxyCommand(payload)

    def _post_find(self, msg):
        subsys = self.get_spdk_subsystems()[msg['nqn']]
        return self._subsystem_to_dict(subsys) if subsys else {}

    def _post_list(self, msg):
        subsystems = self.get_spdk_subsystems()
        return [{'nqn': nqn, **self._subsystem_to_dict(subsys)}
                for nqn, subsys in subsystems.items()]

    def _post_host_list(self, msg):
        subsys = self.get_spdk_subsystems().get(msg['nqn'])
        if subsys is None:
            return {'error': 'nqn not found'}
        elif subsys.get('allow_any_host'):
            return '*'
        return subsys.get('hosts', [])

    def _expand_leave(self, msg):
        payload = self.rpc.nvmf_discovery_remove_referral(
            subnqn=msg['nqn'],
            address=dict(
                traddr=msg['addr'], trsvcid=str(msg['port']), trtype='tcp'))
        yield ProxyCommand(payload)

    def _expand_host_add(self, msg):
        host = msg['host']
        if host == '*':
            payload = self.rpc.nvmf_subsystem_allow_any_host(
                nqn=msg['nqn'], allow_any_host=True)
            yield ProxyCommand(payload)
        else:
            payload = self.rpc.nvmf_subsystem_add_host(
                nqn=msg['nqn'], host=host)
            yield ProxyAddHost(payload, msg.get('key'))

    def _expand_host_del(self, msg):
        payload = self.rpc.nvmf_subsystem_remove_host(
            nqn=msg['nqn'], host=msg['host'])
        yield ProxyCommand(payload)


def main():
    parser = argparse.ArgumentParser(description='proxy server for SPDK')
    parser.add_argument('port', help='proxy server port', type=int)
    parser.add_argument('cmdfile', help='path to file to save commands',
                        type=str, default='/var/lib/nvme-of/cmds.json')
    parser.add_argument('external_addr', help='external address for listeners',
                        type=str, default='0.0.0.0')
    parser.add_argument('-s', dest='sock', help='local socket for RPC',
                        type=str, default='/var/tmp/spdk.sock')
    args = parser.parse_args()
    proxy = Proxy(args.port, args.cmdfile, args.external_addr, args.sock)
    proxy.serve()


if __name__ == '__main__':
    main()
