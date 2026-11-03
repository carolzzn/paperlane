"""Small, bounded IPP client. No discovery or destination supplied by web users."""
import http.client
import ipaddress
import socket
import struct
import urllib.parse


def attribute(tag, name, value):
    name = name.encode()
    if isinstance(value, dict):
        result = bytes([tag]) + struct.pack('>H', len(name)) + name + b'\0\0'
        for key, (child_tag, child_value) in value.items():
            result += attribute(0x4a, '', key) + attribute(child_tag, '', child_value)
        return result + attribute(0x37, '', b'')
    if isinstance(value, bool):
        value = bytes([value])
    elif isinstance(value, int):
        value = struct.pack('>i', value)
    elif isinstance(value, str):
        value = value.encode()
    return bytes([tag]) + struct.pack('>H', len(name)) + name + struct.pack('>H', len(value)) + value


def decode(data):
    if len(data) < 8:
        raise ValueError('打印机返回了不完整的 IPP 响应')
    status = struct.unpack('>H', data[2:4])[0]
    pos, groups, current, last = 8, [], {}, ''

    def value(tag, raw):
        if tag in (0x21, 0x23) and len(raw) == 4:
            return struct.unpack('>i', raw)[0]
        if tag == 0x22:
            return raw != b'\0'
        if tag == 0x32 and len(raw) == 9:
            x, y, units = struct.unpack('>iiB', raw)
            return {'x': x, 'y': y, 'units': units}
        if tag == 0x33 and len(raw) == 8:
            return list(struct.unpack('>ii', raw))
        return raw.decode('utf-8', errors='replace')

    def record():
        nonlocal pos
        if pos + 3 > len(data):
            raise ValueError('IPP 响应被截断')
        tag = data[pos]
        n = struct.unpack('>H', data[pos + 1:pos + 3])[0]
        pos += 3
        if pos + n + 2 > len(data):
            raise ValueError('IPP 名称被截断')
        name = data[pos:pos + n].decode(errors='replace')
        pos += n
        size = struct.unpack('>H', data[pos:pos + 2])[0]
        pos += 2
        if pos + size > len(data):
            raise ValueError('IPP 属性被截断')
        raw = data[pos:pos + size]
        pos += size
        return tag, name, raw

    def collection(depth=0):
        if depth > 12:
            raise ValueError('IPP 集合嵌套过深')
        result, member = {}, ''
        while pos < len(data):
            tag, name, raw = record()
            if tag == 0x37:
                return result
            if tag == 0x4a:
                member = raw.decode(errors='replace')
            else:
                v = collection(depth + 1) if tag == 0x34 else value(tag, raw)
                result.setdefault(member or name, []).append(v)
        raise ValueError('IPP 集合未结束')

    while pos < len(data):
        tag = data[pos]
        if tag == 3:
            break
        if tag <= 0x0f:
            pos += 1
            current, last = {}, ''
            groups.append((tag, current))
            continue
        tag, name, raw = record()
        last = name or last
        v = collection() if tag == 0x34 else value(tag, raw)
        current.setdefault(last, []).append(v)
    if status >= 0x0400:
        messages = [g.get('status-message', [''])[0] for _, g in groups]
        raise ValueError('打印机拒绝请求 (IPP %04x)：%s' % (status, ' '.join(messages)))
    return {'status': status, 'groups': groups, 'attributes': {k: v for _, g in groups for k, v in g.items()}}


class Printer:
    def __init__(self, uri, timeout=15, lan_only=False):
        self.uri = uri
        self.url = urllib.parse.urlsplit(uri)
        if self.url.scheme not in ('ipp', 'ipps') or not self.url.hostname:
            raise ValueError('打印机地址必须是 ipp:// 或 ipps://')
        if self.url.username or self.url.password or self.url.query or self.url.fragment or any(ord(c) < 32 for c in uri):
            raise ValueError('打印机地址不能含账号、密码或查询参数')
        port = self.url.port or 631
        if port not in (443, 631) and not 1024 <= port <= 65535:
            raise ValueError('请使用 IPP 打印服务端口')
        self.lan_only = lan_only
        self.timeout = timeout

    def destination(self):
        if not self.lan_only:
            return None
        addresses = socket.getaddrinfo(self.url.hostname, self.url.port or 631, type=socket.SOCK_STREAM)
        networks = [ipaddress.ip_network(n) for n in ('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16', '100.64.0.0/10', 'fc00::/7', 'fe80::/10')]
        for family, kind, protocol, _, address in addresses:
            ip = ipaddress.ip_address(address[0].split('%')[0])
            if not any(ip in n for n in networks):
                raise ValueError('仅支持家庭局域网或 Tailscale 内的打印机地址')
        if not addresses:
            raise ValueError('无法解析打印机地址')
        return addresses[0]

    def request(self, operation, operation_attrs=(), job_attrs=(), document=b''):
        body = struct.pack('>BBHI', 2, 0, operation, 1) + b'\x01'
        body += attribute(0x47, 'attributes-charset', 'utf-8')
        body += attribute(0x48, 'attributes-natural-language', 'en')
        body += attribute(0x45, 'printer-uri', self.uri)
        for args in operation_attrs:
            body += attribute(*args)
        if job_attrs:
            body += b'\x02'
            for args in job_attrs:
                body += attribute(*args)
        body += b'\x03' + document
        cls = http.client.HTTPSConnection if self.url.scheme == 'ipps' else http.client.HTTPConnection
        conn = cls(self.url.hostname, self.url.port or 631, timeout=self.timeout)
        destination = self.destination()
        if destination:
            # Pin the checked DNS result; HTTPSConnection still verifies TLS
            # using the original hostname when wrapping this socket.
            def connect_checked(address, timeout, source_address=None, **kwargs):
                family, kind, protocol, _, target = destination
                sock = socket.socket(family, kind, protocol)
                sock.settimeout(timeout)
                try:
                    sock.connect(target)
                    return sock
                except BaseException:
                    sock.close()
                    raise
            conn._create_connection = connect_checked
        try:
            conn.request('POST', self.url.path or '/', body, {'Content-Type': 'application/ipp'})
            response = conn.getresponse()
            payload = response.read(4 * 1024 * 1024 + 1)
            if response.status != 200 or len(payload) > 4 * 1024 * 1024:
                raise ValueError('打印机 HTTP 响应异常：%s' % response.status)
            return decode(payload)
        finally:
            conn.close()

    def capabilities(self):
        names = ('printer-name', 'printer-state', 'printer-state-reasons', 'printer-is-accepting-jobs', 'queued-job-count', 'operations-supported', 'document-format-supported', 'media-supported', 'media-ready', 'media-col-database', 'media-col-ready', 'media-size-supported', 'media-left-margin-supported', 'media-right-margin-supported', 'media-top-margin-supported', 'media-bottom-margin-supported', 'printer-resolution-supported', 'printer-resolution-default', 'print-scaling-supported', 'print-quality-supported', 'jpeg-k-octets-supported')
        return self.request(0x000b, [(0x44, 'requested-attributes' if i == 0 else '', n) for i, n in enumerate(names)])['attributes']

    def ticket_attributes(self, ticket):
        m, r = ticket['media'], ticket['resolution']
        collection = {'media-size': (0x34, {'x-dimension': (0x21, m['width']), 'y-dimension': (0x21, m['height'])})}
        for edge, value in zip(('left', 'top', 'right', 'bottom'), m['margins']):
            collection['media-' + edge + '-margin'] = (0x21, value)
        for name in ('type', 'source'):
            if m.get('media_' + name):
                collection['media-' + name] = (0x44, m['media_' + name])
        attrs = [(0x34, 'media-col', collection), (0x44, 'print-scaling', 'none'), (0x23, 'orientation-requested', 3), (0x32, 'printer-resolution', struct.pack('>iiB', r['x'], r['y'], r['units']))]
        if ticket.get('quality'):
            attrs.append((0x23, 'print-quality', ticket['quality']))
        return attrs

    def ticket_request(self, operation, ticket, document=b''):
        result = self.request(operation, [(0x22, 'ipp-attribute-fidelity', True), (0x49, 'document-format', 'image/jpeg'), (0x42, 'requesting-user-name', 'Paperlane'), (0x42, 'job-name', 'Photo')], self.ticket_attributes(ticket), document)
        if result['status'] != 0 or any(group == 5 and values for group, values in result['groups']):
            raise ValueError('设备忽略或替换了打印参数，请重新选择相纸和分辨率')
        return result['attributes']

    def validate_ticket(self, ticket):
        return self.ticket_request(4, ticket)

    def print_ticket(self, data, ticket):
        return self.ticket_request(2, ticket, data)

    def photo_attributes(self, media, landscape=False, dimensions=(10160, 15240)):
        return [(0x44, 'media', media), (0x34, 'media-col', {
            'media-size': (0x34, {'x-dimension': (0x21, dimensions[0]), 'y-dimension': (0x21, dimensions[1])}),
            'media-left-margin': (0x21, 0), 'media-right-margin': (0x21, 0),
            'media-top-margin': (0x21, 0), 'media-bottom-margin': (0x21, 0)}),
            (0x23, 'orientation-requested', 4 if landscape else 3),
            (0x23, 'print-quality', 5), (0x44, 'print-scaling', 'none'),
            (0x32, 'printer-resolution', struct.pack('>iiB', 300, 300, 3))]

    def validate(self, media, landscape=False, dimensions=(10160, 15240)):
        return self.request(0x0004, [(0x49, 'document-format', 'image/jpeg')], self.photo_attributes(media, landscape, dimensions))

    def print_jpeg(self, data, media, landscape=False, dimensions=(10160, 15240)):
        return self.request(0x0002, [(0x42, 'requesting-user-name', 'PhotoStudio'), (0x42, 'job-name', 'Family photo'), (0x49, 'document-format', 'image/jpeg')], self.photo_attributes(media, landscape, dimensions), data)['attributes']

    def job(self, job_id):
        return self.request(0x0009, [(0x21, 'job-id', job_id)])['attributes']
