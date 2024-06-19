import json
import select
import socket


class MockSPDK:
    def __init__(self, sock):
        self.sock = sock
        self.bdevs = {}
        self.clusters = set()
        self.subsystems = {}
        self.referrals = []
        self.handlers = {
            'nvmf_create_transport': self._mock_create_transport,
            'nvmf_get_subsystems': self._mock_get_subsystems,
            'bdev_rbd_register_cluster': self._mock_register_cluster,
            'bdev_rbd_create': self._mock_rbd_create,
            'nvmf_create_subsystem': self._mock_create_subsystem,
            'nvmf_subsystem_add_listener': self._mock_add_listener,
            'nvmf_subsystem_add_ns': self._mock_add_ns,
            'nvmf_subsystem_remove_ns': self._mock_remove_ns,
            'nvmf_delete_subsystem': self._mock_delete_subsystem,
            'nvmf_discovery_add_referral': self._mock_add_referral,
            'nvmf_discovery_remove_referral': self._mock_remove_referral,
        }

    def close(self):
        self.sock.close()

    def _find_subsys(self, nqn):
        return self.subsystems.get(nqn, b'{"error":"subsystem not found"}')

    def _mock_create_transport(self, _):
        pass

    def _mock_register_cluster(self, params):
        name = params['name']
        if name in self.clusters:
            return b'{"error": "cluster already registered"}'
        self.clusters.add(name)

    def _mock_rbd_create(self, params):
        cluster = params['cluster_name']
        if cluster not in self.clusters:
            return b'{"error":"cluster not found"}'
        self.bdevs.setdefault(params['name'],
                              {'pool': params['pool_name'],
                               'image': params['rbd_name'],
                               'cluster': params['cluster_name']})

    def _mock_create_subsystem(self, params):
        dfl = {'listen_addresses': [], 'namespaces': [None]}
        self.subsystems.setdefault(params['nqn'], dfl)

    def _mock_add_listener(self, params):
        addr = params['listen_address']
        try:
            socket.inet_pton(socket.AF_INET, addr['traddr'])
            int(addr['trsvcid'])
        except Exception:
            return b'{"error":"invalid parameters"}'

        if addr['adrfam'].lower() != 'ipv4':
            return b'{"error":"invalid address family"}'

        subsys = self._find_subsys(params['nqn'])
        if isinstance(subsys, bytes):
            return subsys

        params = params.copy()
        del params['listen_address']
        params.update(addr)
        subsys['listen_addresses'].append(params)

    def _mock_add_ns(self, params):
        subsys = self._find_subsys(params['nqn'])
        if isinstance(subsys, bytes):
            return subsys

        subsys['namespaces'][0] =\
            {'nsid': 1, 'name': params['namespace']['bdev_name']}

    def _mock_remove_ns(self, params):
        subsys = self._find_subsys(params['nqn'])
        if isinstance(subsys, bytes):
            return subsys

        subsys['namespaces'][0] = None

    def _mock_delete_subsystem(self, params):
        del self.subsystems[params['nqn']]

    def _mock_add_referral(self, params):
        self.referrals.append(params)

    def _mock_remove_referral(self, params):
        for i, rf in enumerate(self.referrals):
            if (rf['nqn'] == params['nqn'] and
                    rf['addr'] == params['addr'] and
                    rf['port'] == params['port']):
                del self.referrals[i]
                return

    def _mock_get_subsystems(self, params):
        ret = list({'nqn': nqn, **value}
                   for nqn, value in self.subsystems.items())
        return json.dumps({'result': ret}).encode('utf8')

    def loop(self, timeout=None):
        rd, _, _ = select.select([self.sock], [], [], timeout)
        if not rd:
            return False

        buf = self.sock.recv(2048)
        obj = json.loads(buf)

        method = obj['method']
        handler = self.handlers.get(method)
        if handler is None:
            self.sock.sendall(b'{"error":"method not found"}')
            return True

        try:
            rv = handler(obj.get('params', {}))
            if rv is None:
                rv = b'{"result":0}'
            self.sock.sendall(rv)
        except Exception:
            self.sock.sendall(b'{"error":"unexpected error"}')

        return True